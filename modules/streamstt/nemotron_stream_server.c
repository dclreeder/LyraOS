// nemotron_stream_server.c - streaming ASR sidecar for Lyra's RTC mode.
//
// Wraps parakeet.cpp's cache-aware streaming session for
// nvidia/nemotron-3.5-asr-streaming-0.6b (GGUF) in a tiny TCP
// server the core talks to. One client at a time (RTC has exactly
// one peer). The model loads once at startup; each connection
// gets a fresh stream; each flush finalizes the utterance and
// resets the stream for the next one.
//
// Wire protocol (client -> server): framed messages,
//   1-byte type + 4-byte big-endian length + payload
//   'A'  payload = s16le 16 kHz mono PCM
//   'F'  flush: finalize the current utterance, reply with the
//        final line, reset the stream
//   'R'  reset: discard the current utterance, reset the stream
//   'P'  ping: reply {"type":"pong"}
// Server -> client: newline-delimited JSON:
//   {"type":"ready","kind":<n>,"info":{...}}        on connect
//   {"type":"text","text":"<delta>","last_word_end":<sec|null>}
//        newly finalized text (control tokens stripped)
//   {"type":"final","text":"<full utterance text>"}   flush reply
//   {"type":"reset"} / {"type":"pong"} / {"type":"error",...}
//
// The model appends a locale token ("<en-US>") as a pseudo-word
// at finalize; angle-bracket control tokens are stripped from all
// text here so they can never reach the router. The model fires
// no EOU events in practice (bench 2026-11-18); turn end is the
// caller's decision, driven by the finalized-text stream and the
// word timestamps reported here.
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "parakeet_capi.h"

#define DEFAULT_MODEL "/home/dclreeder/Documents/parakeet.cpp/models/nemotron-3.5-asr-streaming-0.6b-q8_0.gguf"
#define DEFAULT_PORT 8097
#define FEED_SAMPLES 1600 /* 100 ms at 16 kHz, the bench's chunk */
#define MAX_FRAME (1u << 22)

static parakeet_ctx *g_ctx;

/* ---------- small dynamic string ---------- */
typedef struct { char *buf; size_t len, cap; } str_t;
static void str_init(str_t *s) { s->cap = 256; s->len = 0; s->buf = malloc(s->cap); s->buf[0] = 0; }
static void str_clear(str_t *s) { s->len = 0; s->buf[0] = 0; }
static void str_catn(str_t *s, const char *p, size_t n) {
    if (s->len + n + 1 > s->cap) { while (s->len + n + 1 > s->cap) s->cap *= 2; s->buf = realloc(s->buf, s->cap); }
    memcpy(s->buf + s->len, p, n); s->len += n; s->buf[s->len] = 0;
}
static void str_cat(str_t *s, const char *p) { str_catn(s, p, strlen(p)); }
static void str_free(str_t *s) { free(s->buf); s->buf = NULL; s->len = s->cap = 0; }

/* ---------- socket io ---------- */
static int read_all(int fd, void *buf, size_t n) {
    char *p = buf;
    while (n > 0) { ssize_t r = recv(fd, p, n, 0); if (r == 0) return 0; if (r < 0) { if (errno == EINTR) continue; return -1; } p += r; n -= (size_t)r; }
    return 1;
}
static int write_all(int fd, const void *buf, size_t n) {
    const char *p = buf;
    while (n > 0) { ssize_t r = send(fd, p, n, MSG_NOSIGNAL); if (r < 0) { if (errno == EINTR) continue; return -1; } p += r; n -= (size_t)r; }
    return 0;
}
static int send_line(int fd, const char *line) { return write_all(fd, line, strlen(line)) == 0 && write_all(fd, "\n", 1) == 0 ? 0 : -1; }

/* JSON-escape a string into out (caller frees via str_free). */
static void json_escape(str_t *out, const char *in) {
    for (const unsigned char *p = (const unsigned char *)in; *p; p++) {
        char tmp[8];
        switch (*p) {
            case '"': str_cat(out, "\\\""); break;
            case '\\': str_cat(out, "\\\\"); break;
            case '\n': str_cat(out, "\\n"); break;
            case '\r': str_cat(out, "\\r"); break;
            case '\t': str_cat(out, "\\t"); break;
            default:
                if (*p < 0x20) { snprintf(tmp, sizeof tmp, "\\u%04x", *p); str_cat(out, tmp); }
                else str_catn(out, (const char *)p, 1);
        }
    }
}

/* Parse a JSON string starting at p (which points at the opening
   quote). Returns malloc'd unescaped string, or NULL. */
static char *parse_json_string(const char *p) {
    if (*p != '"') return NULL;
    p++;
    str_t out; str_init(&out);
    while (*p && *p != '"') {
        if (*p == '\\' && p[1]) {
            p++;
            switch (*p) {
                case 'n': str_cat(&out, "\n"); break;
                case 't': str_cat(&out, "\t"); break;
                case 'r': str_cat(&out, "\r"); break;
                case 'b': str_cat(&out, "\b"); break;
                case 'f': str_cat(&out, "\f"); break;
                case '/': str_cat(&out, "/"); break;
                case '\\': str_cat(&out, "\\"); break;
                case '"': str_cat(&out, "\""); break;
                case 'u': {
                    unsigned cp = (unsigned)strtoul(p + 1, NULL, 16);
                    char u8[4]; int n = 0;
                    if (cp < 0x80) { u8[n++] = (char)cp; }
                    else if (cp < 0x800) { u8[n++] = (char)(0xC0 | (cp >> 6)); u8[n++] = (char)(0x80 | (cp & 0x3F)); }
                    else { u8[n++] = (char)(0xE0 | (cp >> 12)); u8[n++] = (char)(0x80 | ((cp >> 6) & 0x3F)); u8[n++] = (char)(0x80 | (cp & 0x3F)); }
                    str_catn(&out, u8, (size_t)n);
                    p += 4;
                    break;
                }
                default: str_catn(&out, p, 1); break;
            }
            p++;
        } else { str_catn(&out, p, 1); p++; }
    }
    if (*p != '"') { str_free(&out); return NULL; }
    return out.buf;
}

/* Extract the top-level "text" delta from a feed/finalize JSON doc. */
static char *extract_text(const char *json) {
    const char *k = strstr(json, "\"text\"");
    if (!k) return NULL;
    k = strchr(k + 6, ':');
    if (!k) return NULL;
    k++;
    while (*k == ' ' || *k == '\t') k++;
    return parse_json_string(k);
}

/* Scan the "words" array; return the max "end" over words whose
   text is not an angle-bracket control token. -1 if none. */
static double scan_words_last_end(const char *json) {
    const char *w = strstr(json, "\"words\"");
    if (!w) return -1;
    w = strchr(w, '[');
    if (!w) return -1;
    const char *close = strchr(w, ']');
    if (!close) return -1;
    double best = -1;
    const char *p = w;
    while (p < close) {
        const char *wk = strstr(p, "\"w\"");
        if (!wk || wk > close) break;
        const char *colon = strchr(wk + 3, ':');
        if (!colon || colon > close) break;
        const char *q = colon + 1;
        while (*q == ' ' || *q == '\t') q++;
        char *word = parse_json_string(q);
        if (!word) break;
        const char *ek = strstr(q, "\"end\"");
        if (ek && ek < close) {
            const char *ec = strchr(ek + 5, ':');
            if (ec && ec < close) {
                double e = strtod(ec + 1, NULL);
                if (word[0] != '<' && e > best) best = e;
            }
        }
        free(word);
        p = q + 1;
    }
    return best;
}

/* ---------- per-connection stream state ---------- */
typedef struct {
    parakeet_stream *stream;
    float *pcm; size_t pcm_len, pcm_cap;   /* fed in FEED_SAMPLES chunks */
    str_t text;                            /* accumulated stripped text */
    str_t hold;                            /* unterminated '<...' fragment */
    double last_word_end;                  /* seconds, -1 = none yet */
} conn_t;

static void conn_clear_utterance(conn_t *c) {
    c->pcm_len = 0;
    str_clear(&c->text);
    str_clear(&c->hold);
    c->last_word_end = -1;
}

/* Strip <...> control tokens from a delta, holding back an
   unterminated trailing fragment for the next delta. Appends the
   cleaned text to out. */
static void strip_delta(conn_t *c, const char *delta, str_t *out) {
    str_cat(&c->hold, delta);
    const char *p = c->hold.buf;
    size_t consumed = 0;
    for (size_t i = 0; p[i]; i++) {
        if (p[i] == '<') {
            const char *gt = strchr(p + i, '>');
            if (!gt) break;              /* hold from '<' onward */
            i = (size_t)(gt - p);        /* skip the whole token */
            consumed = i + 1;
            continue;
        }
        str_catn(out, p + i, 1);
        consumed = i + 1;
    }
    if (consumed > 0) {
        memmove(c->hold.buf, c->hold.buf + consumed, c->hold.len - consumed + 1);
        c->hold.len -= consumed;
    }
}

/* Fold one feed/finalize JSON doc into the utterance state.
   Returns 1 if the accumulated text changed. */
static int fold_json(conn_t *c, const char *json) {
    int changed = 0;
    char *delta = extract_text(json);
    if (delta) {
        str_t clean; str_init(&clean);
        strip_delta(c, delta, &clean);
        if (clean.len > 0) { str_cat(&c->text, clean.buf); changed = 1; }
        str_free(&clean);
        free(delta);
    }
    double e = scan_words_last_end(json);
    if (e > c->last_word_end) c->last_word_end = e;
    return changed;
}

static int emit_text_line(int fd, conn_t *c, const char *delta_clean) {
    str_t line; str_init(&line);
    str_cat(&line, "{\"type\":\"text\",\"text\":\"");
    str_t esc; str_init(&esc); json_escape(&esc, delta_clean); str_cat(&line, esc.buf); str_free(&esc);
    if (c->last_word_end >= 0) {
        char num[40]; snprintf(num, sizeof num, "\",\"last_word_end\":%.3f}", c->last_word_end);
        str_cat(&line, num);
    } else str_cat(&line, "\",\"last_word_end\":null}");
    int rc = send_line(fd, line.buf);
    str_free(&line);
    return rc;
}

/* Feed buffered PCM in FEED_SAMPLES chunks; emit text lines for
   chunks that produced text (unless quiet). Returns 0 ok, -1 io. */
static int feed_buffered(int fd, conn_t *c, int quiet) {
    while (c->pcm_len >= FEED_SAMPLES) {
        char *j = parakeet_capi_stream_feed_json(c->stream, c->pcm, FEED_SAMPLES);
        memmove(c->pcm, c->pcm + FEED_SAMPLES, (c->pcm_len - FEED_SAMPLES) * sizeof(float));
        c->pcm_len -= FEED_SAMPLES;
        if (!j) return 0; /* model hiccup: drop the chunk, keep the stream */
        size_t before = c->text.len;
        int changed = fold_json(c, j);
        parakeet_capi_free_string(j);
        if (changed && !quiet) {
            if (emit_text_line(fd, c, c->text.buf + before) < 0) return -1;
        }
    }
    return 0;
}

static int do_flush(int fd, conn_t *c) {
    if (c->pcm_len > 0) {
        char *j = parakeet_capi_stream_feed_json(c->stream, c->pcm, (int)c->pcm_len);
        c->pcm_len = 0;
        if (j) { fold_json(c, j); parakeet_capi_free_string(j); }
    }
    char *j = parakeet_capi_stream_finalize_json(c->stream);
    if (j) { fold_json(c, j); parakeet_capi_free_string(j); }
    str_clear(&c->hold); /* any held fragment is a control token */
    /* trim the accumulated text */
    char *t = c->text.buf;
    while (*t == ' ' || *t == '\t') t++;
    size_t n = strlen(t);
    while (n > 0 && (t[n - 1] == ' ' || t[n - 1] == '\t')) t[--n] = 0;
    str_t line; str_init(&line);
    str_cat(&line, "{\"type\":\"final\",\"text\":\"");
    str_t esc; str_init(&esc); json_escape(&esc, t); str_cat(&line, esc.buf); str_free(&esc);
    str_cat(&line, "\"}");
    int rc = send_line(fd, line.buf);
    str_free(&line);
    parakeet_capi_stream_reset(c->stream);
    conn_clear_utterance(c);
    return rc;
}

static void serve_conn(int fd) {
    conn_t c; memset(&c, 0, sizeof c);
    c.stream = parakeet_capi_stream_begin_lang(g_ctx, "en");
    if (!c.stream) {
        fprintf(stderr, "stream_begin failed: %s\n", parakeet_capi_last_error(g_ctx));
        send_line(fd, "{\"type\":\"error\",\"message\":\"stream_begin failed\"}");
        return;
    }
    str_init(&c.text); str_init(&c.hold);
    c.last_word_end = -1;
    char *info = parakeet_capi_stream_info_json(c.stream);
    {
        str_t line; str_init(&line);
        char hdr[64]; snprintf(hdr, sizeof hdr, "{\"type\":\"ready\",\"kind\":%d,\"info\":", parakeet_capi_stream_kind(g_ctx));
        str_cat(&line, hdr);
        str_cat(&line, info ? info : "null");
        str_cat(&line, "}");
        send_line(fd, line.buf);
        str_free(&line);
    }
    if (info) parakeet_capi_free_string(info);
    fprintf(stderr, "client connected\n");
    for (;;) {
        unsigned char hdr[5];
        int r = read_all(fd, hdr, 5);
        if (r <= 0) break;
        uint32_t len = ((uint32_t)hdr[1] << 24) | ((uint32_t)hdr[2] << 16) | ((uint32_t)hdr[3] << 8) | hdr[4];
        if (len > MAX_FRAME) break;
        char *payload = malloc(len ? len : 1);
        if (len > 0 && read_all(fd, payload, len) <= 0) { free(payload); break; }
        int dead = 0;
        switch (hdr[0]) {
            case 'A': {
                size_t nsamp = len / 2;
                if (c.pcm_len + nsamp > c.pcm_cap) {
                    size_t nc = (c.pcm_len + nsamp) * 2 + FEED_SAMPLES;
                    c.pcm = realloc(c.pcm, nc * sizeof(float)); c.pcm_cap = nc;
                }
                const int16_t *s16 = (const int16_t *)payload;
                for (size_t i = 0; i < nsamp; i++) c.pcm[c.pcm_len++] = s16[i] / 32768.0f;
                if (feed_buffered(fd, &c, 0) < 0) dead = 1;
                break;
            }
            case 'F': if (do_flush(fd, &c) < 0) dead = 1; break;
            case 'R':
                parakeet_capi_stream_reset(c.stream);
                conn_clear_utterance(&c);
                if (send_line(fd, "{\"type\":\"reset\"}") < 0) dead = 1;
                break;
            case 'P': if (send_line(fd, "{\"type\":\"pong\"}") < 0) dead = 1; break;
            default: if (send_line(fd, "{\"type\":\"error\",\"message\":\"unknown frame type\"}") < 0) dead = 1; break;
        }
        free(payload);
        if (dead) break;
    }
    fprintf(stderr, "client disconnected\n");
    parakeet_capi_stream_free(c.stream);
    free(c.pcm); str_free(&c.text); str_free(&c.hold);
}

int main(int argc, char **argv) {
    const char *model = argc > 1 ? argv[1] : DEFAULT_MODEL;
    int port = argc > 2 ? atoi(argv[2]) : DEFAULT_PORT;
    signal(SIGPIPE, SIG_IGN);
    g_ctx = parakeet_capi_load(model);
    if (!g_ctx) { fprintf(stderr, "model load failed: %s\n", parakeet_capi_load_error()); return 1; }
    fprintf(stderr, "model loaded: %s (stream kind %d)\n", model, parakeet_capi_stream_kind(g_ctx));
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) { perror("socket"); return 1; }
    int one = 1; setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in addr; memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET; addr.sin_port = htons((uint16_t)port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(srv, (struct sockaddr *)&addr, sizeof addr) < 0) { perror("bind"); return 1; }
    if (listen(srv, 4) < 0) { perror("listen"); return 1; }
    fprintf(stderr, "listening on 127.0.0.1:%d\n", port);
    for (;;) {
        int fd = accept(srv, NULL, NULL);
        if (fd < 0) { if (errno == EINTR) continue; perror("accept"); break; }
        int one2 = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one2, sizeof one2);
        serve_conn(fd);
        close(fd);
    }
    return 0;
}
