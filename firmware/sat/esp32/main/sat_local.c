/* Lyra Sat Gadget -- local-path LAN client (sat1).
 *
 * Provenance: adapted 2026-10-30 from the archived Lyra Box satellite
 * firmware (firmware/main/sat_client.c + audio.c), which ran this same
 * Flex/XIAO hardware against the PC bridge for a full product cycle.
 * The framing, incremental parser, reconnect/backoff, heartbeat,
 * jitter FIFO and the 24k->48k streaming upsampler are that code's,
 * re-pointed at this SDK tree's audio layer:
 *   - mic in:  voice_board_mic_read() (16 kHz mono from the XVF3800's
 *     processed channel, via voice_board_lyra_flex.c) instead of the
 *     old raw I2S reads;
 *   - audio out: voice_board_speaker_write() (48 kHz stereo int32)
 *     instead of raw I2S writes. The board applies the device volume
 *     as its gain stage; that volume is the SDK voice stack's NVS
 *     "voice_volume" percent (config_store, owned by voice.c) -- the
 *     device-side volume rule stands: volume lives here, persisted in
 *     NVS, reported in hello/status, never owned by the Fedora;
 *   - CTRL parsing uses cJSON (already linked) instead of the old
 *     hand-rolled extractor; the message set is the transport doc's.
 *
 * Robustness rules carried over unchanged: ANY malformed frame (bad
 * magic, len > SAT_MAX_PAYLOAD, oversize for the RX buffer) drops the
 * connection cleanly and reconnects with backoff -- never a panic,
 * never unbounded parsing. First connect waits for a real IP (GOT_IP):
 * the standing never-connect-before-IP lesson.
 *
 * While an SDK realtime call is active (voice_call_active()), this
 * client yields: it does not read the mic (the call pumps it), drops
 * downlink frames, and silences its speaker task; call_state edges are
 * reported to the Fedora. When the call ends, streaming resumes.
 */
#include "sat_local.h"
#include "sat_proto.h"

#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>

#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_ota_ops.h"  /* sat9: OTA over satlink */
#include "mbedtls/md.h"   /* sat9: streaming SHA-256 (idiom as link_pairing.c) */
#include "esp_system.h"  /* sat5 diag: esp_reset_reason */
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "config_store.h"
#include "voice.h"
#include "voice_board.h"
#include "wifi_mgr.h"

static const char *TAG = "sat_local";

/* RX buffer: audio/CTRL frames are <= 960 B, but sat9 OTA_DATA frames
 * carry up to SAT_OTA_MAX_CHUNK of firmware image, so the parser
 * buffer must hold one whole OTA frame. */
#define RX_BUF_SIZE (SAT_OTA_MAX_CHUNK + 64)

static int s_sock = -1;
static bool s_connected;
static SemaphoreHandle_t s_send_lock;
static uint32_t s_up_seq;
static uint32_t s_down_expected;
static bool s_down_have_seq;
static int64_t s_last_rx_us;
static int64_t s_last_ping_us;
static bool s_started;

/* --- volume: the SDK voice stack's NVS percent (voice.c owns the key) --- */
#define VOLUME_KEY "voice_volume"  /* config_store key used by voice.c */
#define VOLUME_DEFAULT_PCT 60      /* voice.c's DEFAULT_VOLUME */

/* RAM cache of the percent (sat5 fix). The speaker task runs on a
 * PSRAM stack; ANY flash access from it -- even an NVS *read* -- trips
 * the IDF assert esp_task_stack_is_sane_cache_disabled() and panics
 * (that was the sat4 alert-start reset: an NVS read on the
 * speaker task's PSRAM stack). So: NVS reads happen only on
 * internal-stack contexts (net task, start) and refresh this cache;
 * PSRAM-stack code uses volume_pct_cached(), which never touches
 * flash. */
static int s_volume_pct_cache = VOLUME_DEFAULT_PCT;

static int volume_pct(void)
{
    char buf[8];
    if (config_get_str(VOLUME_KEY, buf, sizeof(buf)) && buf[0]) {
        int v = atoi(buf);
        s_volume_pct_cache = v < 0 ? 0 : v > 100 ? 100 : v;
    }
    return s_volume_pct_cache;
}

static int volume_pct_cached(void) { return s_volume_pct_cache; }

static void volume_set(float v01)
{
    if (v01 < 0.0f) v01 = 0.0f;
    if (v01 > 1.0f) v01 = 1.0f;
    int pct = (int)lroundf(v01 * 100.0f);
    char buf[8];
    snprintf(buf, sizeof(buf), "%d", pct);
    if (!config_set_str(VOLUME_KEY, buf))
        ESP_LOGW(TAG, "volume could not be stored");
    s_volume_pct_cache = pct;
    voice_board_set_volume(pct);  /* the board's gain stage applies it */
    ESP_LOGI(TAG, "volume -> %d%%", pct);
}

/* --- downlink jitter FIFO + 24k->48k upsampler (archived audio.c) ------ */
#define FIFO_FRAMES  10
#define FIFO_PREFILL 5  /* ~100 ms; transport budget is ~120 ms */
static int16_t s_fifo[FIFO_FRAMES][SAT_DOWN_SAMPLES];
static int s_fifo_head, s_fifo_tail, s_fifo_count;
static bool s_playing;
static SemaphoreHandle_t s_fifo_lock;
static uint32_t s_down_gaps;

static void fifo_push(const int16_t *samples)
{
    xSemaphoreTake(s_fifo_lock, portMAX_DELAY);
    if (s_fifo_count == FIFO_FRAMES) {  /* overflow: drop oldest */
        s_fifo_tail = (s_fifo_tail + 1) % FIFO_FRAMES;
        s_fifo_count--;
        s_down_gaps++;
    }
    memcpy(s_fifo[s_fifo_head], samples, SAT_DOWN_BYTES);
    s_fifo_head = (s_fifo_head + 1) % FIFO_FRAMES;
    s_fifo_count++;
    xSemaphoreGive(s_fifo_lock);
}

static void fifo_flush(void)
{
    xSemaphoreTake(s_fifo_lock, portMAX_DELAY);
    s_fifo_head = s_fifo_tail = s_fifo_count = 0;
    s_playing = false;
    xSemaphoreGive(s_fifo_lock);
}

/* Streaming linear interpolation 24k -> 48k (archived Upsampler2448:
 * emits midpoint(prev, cur) then cur, carrying prev across frames so
 * there is no seam at frame boundaries). */
typedef struct {
    int16_t prev;
    bool have_prev;
} upsampler2448_t;
static upsampler2448_t s_ups;

static size_t upsampler2448_process(upsampler2448_t *u, const int16_t *in,
                                    size_t in_samples, int16_t *out,
                                    size_t out_capacity)
{
    size_t produced = 0;
    for (size_t i = 0; i < in_samples && produced + 1 < out_capacity; i++) {
        int16_t cur = in[i];
        if (!u->have_prev) {
            out[produced++] = cur;
            out[produced++] = cur;
        } else {
            out[produced++] = (int16_t)(((int32_t)u->prev + cur) / 2);
            out[produced++] = cur;
        }
        u->prev = cur;
        u->have_prev = true;
    }
    return produced;
}

/* One 20 ms downlink frame -> 960 stereo int32 bus frames @48k. Volume
 * is NOT applied here: voice_board_speaker_write applies the board
 * gain (the NVS volume above), exactly like the SDK's own player. */
#define BUS_FRAMES_OUT 960  /* 20 ms at 48 kHz */
static int16_t s_up_buf[BUS_FRAMES_OUT + 2];
static int32_t s_tx_buf[BUS_FRAMES_OUT * 2];

static void speaker_task(void *arg)
{
    (void)arg;
    static int16_t frame[SAT_DOWN_SAMPLES];
    for (;;) {
        if (!voice_ready() || voice_call_active()) {
            /* Board not up yet, or the call owns the speaker. */
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        bool have_frame = false;
        xSemaphoreTake(s_fifo_lock, portMAX_DELAY);
        if (!s_playing && s_fifo_count >= FIFO_PREFILL) s_playing = true;
        if (s_playing) {
            if (s_fifo_count > 0) {
                memcpy(frame, s_fifo[s_fifo_tail], SAT_DOWN_BYTES);
                s_fifo_tail = (s_fifo_tail + 1) % FIFO_FRAMES;
                s_fifo_count--;
                have_frame = true;
            } else {
                s_playing = false;  /* underrun: rebuffer before playing */
            }
        }
        xSemaphoreGive(s_fifo_lock);
        if (!have_frame) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        memset(s_up_buf, 0, sizeof(s_up_buf));
        upsampler2448_process(&s_ups, frame, SAT_DOWN_SAMPLES,
                              s_up_buf, BUS_FRAMES_OUT + 2);
        for (int i = 0; i < BUS_FRAMES_OUT; i++) {
            int32_t out = ((int32_t)s_up_buf[i]) << 16;
            s_tx_buf[2 * i] = out;
            s_tx_buf[2 * i + 1] = out;
        }
        /* Blocks as the DMA drains -- this paces the task to realtime. */
        voice_board_speaker_write(s_tx_buf, BUS_FRAMES_OUT);
    }
}

/* --- frame send helpers (archived sat_client.c pattern) ----------------- */
static bool send_all_locked(const uint8_t *buf, size_t len)
{
    size_t sent = 0;
    while (sent < len) {
        int n = send(s_sock, buf + sent, len - sent, 0);
        if (n <= 0) return false;
        sent += (size_t)n;
    }
    return true;
}

static void net_drop(void)
{
    if (s_sock >= 0) shutdown(s_sock, SHUT_RDWR);
}

static bool send_ctrl_json(const char *json)
{
    size_t jlen = strlen(json);
    if (jlen > 1280 || !s_connected) return false;
    /* Static frame buffer (held under s_send_lock): CTRL messages
     * can run longer than the old 512 B cap. */
    static uint8_t out[SAT_HEADER_LEN + 1280];
    xSemaphoreTake(s_send_lock, portMAX_DELAY);
    bool ok = false;
    if (s_sock >= 0) {
        size_t n = sat_pack_frame(out, SAT_F_CTRL, 0,
                                  (uint64_t)esp_timer_get_time(),
                                  (const uint8_t *)json, (uint32_t)jlen);
        ok = send_all_locked(out, n);
    }
    xSemaphoreGive(s_send_lock);
    if (!ok) net_drop();
    return ok;
}

static bool send_audio_up(const int16_t *frame)
{
    if (!s_connected) return false;
    uint8_t out[SAT_HEADER_LEN + SAT_UP_BYTES];
    xSemaphoreTake(s_send_lock, portMAX_DELAY);
    bool ok = false;
    if (s_sock >= 0) {
        s_up_seq++;
        size_t n = sat_pack_frame(out, SAT_F_AUDIO_UP, s_up_seq,
                                  (uint64_t)esp_timer_get_time(),
                                  (const uint8_t *)frame, SAT_UP_BYTES);
        ok = send_all_locked(out, n);
    }
    xSemaphoreGive(s_send_lock);
    if (!ok) net_drop();
    return ok;
}

bool sat_local_connected(void) { return s_connected; }

/* --- CTRL senders -------------------------------------------------------- */
static void send_hello(void)
{
    const esp_app_desc_t *app = esp_app_get_description();
    char json[448];
    snprintf(json, sizeof(json),
             "{\"cmd\":\"hello\",\"device_id\":\"%s\","
             "\"fw\":\"%s\",\"volume\":%.2f,\"xvf_fw\":\"unknown\"}",
             CONFIG_LYRA_SAT_DEVICE_ID, app ? app->version : "?",
             (double)volume_pct() / 100.0);
    send_ctrl_json(json);
}

static void send_call_state(bool active)
{
    char json[64];
    snprintf(json, sizeof(json),
             "{\"cmd\":\"call_state\",\"state\":\"%s\"}",
             active ? "active" : "ended");
    send_ctrl_json(json);
}

static void send_status(void)
{
    wifi_ap_record_t ap;
    int rssi = 0;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) rssi = ap.rssi;
    char json[512];
    snprintf(json, sizeof(json),
             "{\"cmd\":\"status\",\"uptime_s\":%lld,"
             "\"heap_free\":%u,\"heap_internal\":%u,"
             "\"heap_largest_internal\":%u,\"rssi\":%d,"
             "\"call_state\":\"%s\",\"xvf_ok\":%s,"
             "\"volume\":%.2f,\"down_gaps\":%u,\"reset_reason\":%d}",
             (long long)(esp_timer_get_time() / 1000000),
             (unsigned)esp_get_free_heap_size(),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
             rssi, voice_call_active() ? "active" : "idle",
             voice_board_muted() ? "false" : "true",
             (double)volume_pct() / 100.0,
             (unsigned)s_down_gaps,
             (int)esp_reset_reason());
    send_ctrl_json(json);
}

/* --- OTA over satlink (sat9) ----------------------------------------------
 * Wire protocol (host = Fedora Lyra core; frame spec in sat_proto.h):
 *   CTRL {"cmd":"ota_begin","size":N,"sha256":"<64 hex>"}
 *       -> {"cmd":"ota_ready"} | {"cmd":"ota_error","reason":...}
 *          esp_ota_begin on esp_ota_get_next_update_partition().
 *   frame 0x04 OTA_DATA: raw image bytes, <= SAT_OTA_MAX_CHUNK each,
 *       written in arrival order; NO per-frame reply. Every 256 KB
 *       received the box sends {"cmd":"ota_progress","received":N}.
 *   CTRL {"cmd":"ota_end"} -> byte count AND running SHA-256 must
 *       match the ota_begin announcement; then esp_ota_end +
 *       esp_ota_set_boot_partition, {"cmd":"ota_ok"}, reboot.
 *   CTRL {"cmd":"ota_abort"} -> esp_ota_abort, {"cmd":"ota_aborted"}.
 * Safety: the running slot is never written. Any session that ends
 * without a verified ota_end -- link loss (net_task cleanup below),
 * abort, write error, overrun, size/hash mismatch -- aborts the OTA
 * and leaves the boot partition untouched, so a partial or corrupt
 * image can never become bootable. AUDIO_DOWN is ignored while a
 * session runs; ping/pong keep working. */
typedef struct {
    bool active;
    esp_ota_handle_t handle;
    const esp_partition_t *part;
    uint32_t size;          /* announced total bytes */
    uint32_t received;
    uint32_t next_progress; /* next ota_progress threshold */
    uint8_t sha_expected[32];
    mbedtls_md_context_t md;
} ota_session_t;
static ota_session_t s_ota;

static void ota_error(const char *reason)
{
    char json[192];
    snprintf(json, sizeof(json),
             "{\"cmd\":\"ota_error\",\"reason\":\"%s\"}", reason);
    send_ctrl_json(json);
}

static void ota_session_clear(bool abort_flash)
{
    if (s_ota.active && abort_flash) esp_ota_abort(s_ota.handle);
    mbedtls_md_free(&s_ota.md);
    memset(&s_ota, 0, sizeof(s_ota));
}

static bool parse_sha256_hex(const char *s, uint8_t out[32])
{
    if (!s || strlen(s) != 64) return false;
    for (int i = 0; i < 32; i++) {
        int hi, lo;
        char a = s[2 * i], b = s[2 * i + 1];
        hi = (a >= '0' && a <= '9') ? a - '0' : (a >= 'a' && a <= 'f')
             ? a - 'a' + 10 : (a >= 'A' && a <= 'F') ? a - 'A' + 10 : -1;
        lo = (b >= '0' && b <= '9') ? b - '0' : (b >= 'a' && b <= 'f')
             ? b - 'a' + 10 : (b >= 'A' && b <= 'F') ? b - 'A' + 10 : -1;
        if (hi < 0 || lo < 0) return false;
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return true;
}

static void ota_begin(const cJSON *root)
{
    if (s_ota.active) { ota_error("already in progress"); return; }
    const cJSON *size_j = cJSON_GetObjectItem(root, "size");
    const cJSON *sha_j = cJSON_GetObjectItem(root, "sha256");
    if (!cJSON_IsNumber(size_j) || size_j->valuedouble <= 0 ||
        !cJSON_IsString(sha_j)) {
        ota_error("bad begin args");
        return;
    }
    uint32_t size = (uint32_t)size_j->valuedouble;
    uint8_t sha[32];
    if (!parse_sha256_hex(sha_j->valuestring, sha)) {
        ota_error("bad sha256");
        return;
    }
    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    if (!part) { ota_error("no update partition"); return; }
    if (size > part->size) { ota_error("image too large"); return; }
    esp_ota_handle_t handle;
    if (esp_ota_begin(part, size, &handle) != ESP_OK) {
        ota_error("esp_ota_begin failed");
        return;
    }
    memset(&s_ota, 0, sizeof(s_ota));
    s_ota.active = true;
    s_ota.handle = handle;
    s_ota.part = part;
    s_ota.size = size;
    s_ota.next_progress = 256 * 1024;
    memcpy(s_ota.sha_expected, sha, sizeof(sha));
    mbedtls_md_init(&s_ota.md);
    const mbedtls_md_info_t *info =
        mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!info || mbedtls_md_setup(&s_ota.md, info, 0) != 0 ||
        mbedtls_md_starts(&s_ota.md) != 0) {
        ota_session_clear(true);
        ota_error("sha init failed");
        return;
    }
    ESP_LOGI(TAG, "ota_begin: %lu bytes -> partition %s @0x%lx",
             (unsigned long)size, part->label,
             (unsigned long)part->address);
    send_ctrl_json("{\"cmd\":\"ota_ready\"}");
}

static void ota_data(const uint8_t *payload, uint32_t len)
{
    if (!s_ota.active) {
        ESP_LOGW(TAG, "OTA_DATA with no session; ignoring");
        return;
    }
    if (len == 0 || s_ota.received + len > s_ota.size) {
        ESP_LOGW(TAG, "OTA overrun; aborting session");
        ota_session_clear(true);
        ota_error("overrun");
        return;
    }
    if (esp_ota_write(s_ota.handle, payload, len) != ESP_OK ||
        mbedtls_md_update(&s_ota.md, payload, len) != 0) {
        ESP_LOGE(TAG, "OTA write/hash failed; aborting session");
        ota_session_clear(true);
        ota_error("write failed");
        return;
    }
    s_ota.received += len;
    if (s_ota.received >= s_ota.next_progress) {
        s_ota.next_progress += 256 * 1024;
        char json[96];
        snprintf(json, sizeof(json),
                 "{\"cmd\":\"ota_progress\",\"received\":%lu}",
                 (unsigned long)s_ota.received);
        send_ctrl_json(json);
    }
}

static void ota_end(void)
{
    if (!s_ota.active) { ota_error("no session"); return; }
    uint8_t digest[32];
    bool ok = s_ota.received == s_ota.size &&
              mbedtls_md_finish(&s_ota.md, digest) == 0 &&
              memcmp(digest, s_ota.sha_expected, sizeof(digest)) == 0;
    if (!ok) {
        ESP_LOGW(TAG, "ota_end verify failed (received %lu of %lu, "
                      "hash %s); aborting",
                 (unsigned long)s_ota.received, (unsigned long)s_ota.size,
                 s_ota.received == s_ota.size ? "mismatch" : "n/a");
        ota_session_clear(true);
        ota_error("verify failed");
        return;
    }
    const esp_partition_t *part = s_ota.part;
    esp_err_t err = esp_ota_end(s_ota.handle);  /* validates the image */
    mbedtls_md_free(&s_ota.md);
    memset(&s_ota, 0, sizeof(s_ota));
    if (err != ESP_OK) { ota_error("esp_ota_end failed"); return; }
    if (esp_ota_set_boot_partition(part) != ESP_OK) {
        ota_error("set boot partition failed");
        return;
    }
    ESP_LOGI(TAG, "ota_end: image verified and bootable; rebooting");
    send_ctrl_json("{\"cmd\":\"ota_ok\"}");
    vTaskDelay(pdMS_TO_TICKS(300));  /* let the reply flush before reset */
    esp_restart();
}

static void ota_abort_cmd(void)
{
    if (s_ota.active) {
        ota_session_clear(true);
        ESP_LOGI(TAG, "ota_abort: session aborted by host");
    }
    send_ctrl_json("{\"cmd\":\"ota_aborted\"}");
}

/* Bootloader rollback (sat9): a freshly OTA'd image boots in state
 * ESP_OTA_IMG_PENDING_VERIFY; unless the app marks it valid, the
 * bootloader rolls back to the previous image on the next reset.
 * "Proven" for this box = the satlink hello handshake completed:
 * the core answered our hello with its own {"cmd":"hello"} (the
 * handle_ctrl branch below), meaning the new image is up, on Wi-Fi,
 * and talking the protocol to the real core. No-op on normal boots
 * (state is VALID, or otadata has no pending image). */
static void maybe_mark_app_valid(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    if (running &&
        esp_ota_get_state_partition(running, &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY &&
        esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {
        ESP_LOGI(TAG, "OTA image marked valid (hello handshake complete)");
    }
}

/* --- CTRL handling -------------------------------------------------------- */
static void handle_ctrl(const uint8_t *payload, uint32_t len)
{
    if (len == 0 || len > 1024) {
        ESP_LOGW(TAG, "CTRL bad size %lu; ignoring", (unsigned long)len);
        return;
    }
    char *text = strndup((const char *)payload, len);
    if (!text) return;
    cJSON *root = cJSON_Parse(text);
    free(text);
    if (!root) {
        ESP_LOGW(TAG, "CTRL not JSON; ignoring");
        return;
    }
    const cJSON *cmd_item = cJSON_GetObjectItem(root, "cmd");
    const char *cmd = cJSON_IsString(cmd_item) ? cmd_item->valuestring : "";
    if (!strcmp(cmd, "ping")) {
        send_ctrl_json("{\"cmd\":\"pong\"}");
    } else if (!strcmp(cmd, "pong")) {
        /* liveness already updated by frame receipt */
    } else if (!strcmp(cmd, "hello")) {
        send_hello();
        maybe_mark_app_valid();  /* sat9 rollback proof point */
    } else if (!strcmp(cmd, "barge_in")) {
        fifo_flush();
        ESP_LOGI(TAG, "barge_in: playout flushed");
    } else if (!strcmp(cmd, "set_volume")) {
        const cJSON *v = cJSON_GetObjectItem(root, "volume");
        if (cJSON_IsNumber(v)) volume_set((float)v->valuedouble);
        else ESP_LOGW(TAG, "set_volume without a numeric volume");
    } else if (!strcmp(cmd, "get_status")) {
        send_status();
    } else if (!strcmp(cmd, "start_call")) {
        if (voice_call_active()) {
            ESP_LOGI(TAG, "start_call ignored: call already active");
        } else {
            ESP_LOGI(TAG, "start_call: requesting SDK call");
            voice_request_call();
        }
    } else if (!strcmp(cmd, "ota_begin")) {
        ota_begin(root);
    } else if (!strcmp(cmd, "ota_end")) {
        ota_end();
    } else if (!strcmp(cmd, "ota_abort")) {
        ota_abort_cmd();
    } else {
        ESP_LOGW(TAG, "unknown CTRL cmd: %s", cmd);
    }
    cJSON_Delete(root);
}

static void dispatch_frame(uint8_t ftype, uint32_t seq,
                           const uint8_t *payload, uint32_t len)
{
    s_last_rx_us = esp_timer_get_time();
    if (ftype == SAT_F_AUDIO_DOWN) {
        if (s_ota.active) return;  /* sat9: downlink yields during OTA */
        if (len != SAT_DOWN_BYTES) {
            ESP_LOGW(TAG, "AUDIO_DOWN wrong size %lu; skipping",
                     (unsigned long)len);
            return;
        }
        if (s_down_have_seq && seq != s_down_expected)
            ESP_LOGW(TAG, "AUDIO_DOWN seq gap: expected %lu got %lu",
                     (unsigned long)s_down_expected, (unsigned long)seq);
        s_down_have_seq = true;
        s_down_expected = seq + 1;
        if (voice_call_active()) return;  /* the call owns the speaker */
        fifo_push((const int16_t *)payload);
    } else if (ftype == SAT_F_OTA_DATA) {
        ota_data(payload, len);
    } else if (ftype == SAT_F_CTRL) {
        handle_ctrl(payload, len);
    } else {
        ESP_LOGW(TAG, "unexpected frame type 0x%02x; ignoring", ftype);
    }
}

/* --- incremental frame parser (archived) ---------------------------------- */
typedef struct {
    bool have_hdr;
    uint8_t hdr[SAT_HEADER_LEN];
    size_t hdr_have;
    uint8_t payload[RX_BUF_SIZE];
    uint32_t payload_len;
    size_t payload_have;
    uint8_t ftype;
    uint32_t seq;
} parser_t;
static parser_t s_parser;

/* Feed raw bytes; returns false on fatal protocol error (drop conn). */
static bool parser_feed(const uint8_t *data, size_t len)
{
    size_t off = 0;
    while (off < len) {
        if (!s_parser.have_hdr) {
            size_t need = SAT_HEADER_LEN - s_parser.hdr_have;
            size_t take = need < len - off ? need : len - off;
            memcpy(s_parser.hdr + s_parser.hdr_have, data + off, take);
            s_parser.hdr_have += take;
            off += take;
            if (s_parser.hdr_have < SAT_HEADER_LEN) break;
            uint16_t magic = sat_get_u16be(s_parser.hdr + SAT_OFF_MAGIC);
            uint32_t plen = sat_get_u32be(s_parser.hdr + SAT_OFF_LEN);
            if (magic != SAT_MAGIC || plen > SAT_MAX_PAYLOAD ||
                plen > RX_BUF_SIZE) {
                ESP_LOGW(TAG, "bad frame (magic 0x%04x len %lu): dropping",
                         magic, (unsigned long)plen);
                return false;
            }
            s_parser.ftype = s_parser.hdr[SAT_OFF_TYPE];
            s_parser.seq = sat_get_u32be(s_parser.hdr + SAT_OFF_SEQ);
            s_parser.payload_len = plen;
            s_parser.payload_have = 0;
            s_parser.have_hdr = true;
            if (plen == 0) {
                dispatch_frame(s_parser.ftype, s_parser.seq, NULL, 0);
                memset(&s_parser, 0, sizeof(s_parser));
            }
        } else {
            size_t need = s_parser.payload_len - s_parser.payload_have;
            size_t take = need < len - off ? need : len - off;
            memcpy(s_parser.payload + s_parser.payload_have, data + off,
                   take);
            s_parser.payload_have += take;
            off += take;
            if (s_parser.payload_have == s_parser.payload_len) {
                dispatch_frame(s_parser.ftype, s_parser.seq,
                               s_parser.payload, s_parser.payload_len);
                memset(&s_parser, 0, sizeof(s_parser));
            }
        }
    }
    return true;
}

/* --- receive loop with heartbeat (archived) -------------------------------- */
static void rx_loop(void)
{
    uint8_t buf[1024];
    memset(&s_parser, 0, sizeof(s_parser));
    s_last_rx_us = s_last_ping_us = esp_timer_get_time();
    for (;;) {
        int n = recv(s_sock, buf, sizeof(buf), 0);
        if (n > 0) {
            if (!parser_feed(buf, (size_t)n)) return;  /* fatal */
            continue;
        }
        if (n == 0) {
            ESP_LOGW(TAG, "peer closed");
            return;
        }
        if (errno != EAGAIN && errno != EWOULDBLOCK) {
            ESP_LOGW(TAG, "recv error %d", errno);
            return;
        }
        int64_t now = esp_timer_get_time();
        if (now - s_last_rx_us > 15000000LL) {
            ESP_LOGW(TAG, "no frames for 15 s; dropping connection");
            return;
        }
        if (now - s_last_ping_us > 5000000LL) {
            send_ctrl_json("{\"cmd\":\"ping\"}");
            s_last_ping_us = now;
        }
    }
}

/* --- mic uplink ------------------------------------------------------------- */
static void mic_task(void *arg)
{
    (void)arg;
    static int16_t frame[SAT_UP_SAMPLES];
    static int16_t chunk[SAT_UP_SAMPLES];
    size_t have = 0;
    for (;;) {
        if (!s_connected || !voice_ready() || voice_call_active()) {
            /* Not streaming: no session, board not up, or the call
             * owns the mic. (We never stop the board mic ourselves:
             * run_call stops it on exit, and a read against a stopped
             * mic just returns 0, which re-arms it below.) */
            have = 0;
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        int peak = 0;
        size_t want = SAT_UP_SAMPLES - have;
        size_t got = voice_board_mic_read(chunk, want, &peak);
        if (!got) {
            voice_board_mic_start();
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        memcpy(frame + have, chunk, got * sizeof(int16_t));
        have += got;
        if (have >= SAT_UP_SAMPLES) {
            send_audio_up(frame);
            have = 0;
        }
    }
}

/* --- call state monitor ------------------------------------------------------ */
static void callmon_task(void *arg)
{
    (void)arg;
    bool was_active = false;
    for (;;) {
        bool active = voice_call_active();
        if (active != was_active) {
            was_active = active;
            if (active) fifo_flush();  /* local speech yields at once */
            if (s_connected) send_call_state(active);
            ESP_LOGI(TAG, "call %s", active ? "active" : "ended");
        }
        vTaskDelay(pdMS_TO_TICKS(250));
    }
}

/* --- connection manager ------------------------------------------------------- */
static bool have_ip(void)
{
    if (!wifi_mgr_is_connected()) return false;
    esp_netif_t *netif = wifi_mgr_get_netif();
    if (!netif) return false;
    esp_netif_ip_info_t ip;
    if (esp_netif_get_ip_info(netif, &ip) != ESP_OK) return false;
    return ip.ip.addr != 0;
}

static void net_task(void *arg)
{
    (void)arg;
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)CONFIG_LYRA_SAT_SERVER_PORT);
    if (inet_pton(AF_INET, CONFIG_LYRA_SAT_SERVER_HOST, &addr.sin_addr) != 1) {
        ESP_LOGE(TAG, "bad LYRA_SAT_SERVER_HOST '%s'",
                 CONFIG_LYRA_SAT_SERVER_HOST);
        vTaskDelete(NULL);
    }
    uint32_t backoff_ms = 1000;
    for (;;) {
        /* GOT_IP gate: never attempt a connect before the station
         * holds a real address (standing lesson). */
        if (!have_ip()) {
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }
        int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
        if (sock < 0) {
            vTaskDelay(pdMS_TO_TICKS(backoff_ms));
            continue;
        }
        struct timeval tmo = {.tv_sec = 2, .tv_usec = 0};
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tmo, sizeof(tmo));
        setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tmo, sizeof(tmo));
        if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
            ESP_LOGW(TAG, "connect to %s:%d failed (errno %d); "
                          "retry in %lu ms",
                     CONFIG_LYRA_SAT_SERVER_HOST,
                     CONFIG_LYRA_SAT_SERVER_PORT, errno,
                     (unsigned long)backoff_ms);
            close(sock);
            vTaskDelay(pdMS_TO_TICKS(backoff_ms));
            if (backoff_ms < 30000) backoff_ms *= 2;
            continue;
        }
        ESP_LOGI(TAG, "connected to Fedora service %s:%d",
                 CONFIG_LYRA_SAT_SERVER_HOST, CONFIG_LYRA_SAT_SERVER_PORT);
        backoff_ms = 1000;
        xSemaphoreTake(s_send_lock, portMAX_DELAY);
        s_sock = sock;
        s_up_seq = 0;
        s_down_have_seq = false;
        s_connected = true;
        xSemaphoreGive(s_send_lock);
        send_hello();
        if (voice_call_active()) send_call_state(true);
        rx_loop();  /* returns only when the connection dies */
        xSemaphoreTake(s_send_lock, portMAX_DELAY);
        s_connected = false;
        if (s_sock >= 0) { close(s_sock); s_sock = -1; }
        xSemaphoreGive(s_send_lock);
        if (s_ota.active) {
            ESP_LOGW(TAG, "link lost mid-OTA; aborting session "
                          "(boot partition untouched)");
            ota_session_clear(true);
        }
        fifo_flush();
        ESP_LOGW(TAG, "disconnected; retry in %lu ms",
                 (unsigned long)backoff_ms);
        vTaskDelay(pdMS_TO_TICKS(backoff_ms));
        if (backoff_ms < 30000) backoff_ms *= 2;
    }
}

void sat_local_start(void)
{
    if (s_started) return;
    s_started = true;
    ESP_LOGW(TAG, "sat5 diag: last reset reason %d", (int)esp_reset_reason());
    volume_pct();        /* prime the RAM caches (sat5 fix) */
    s_send_lock = xSemaphoreCreateMutex();
    s_fifo_lock = xSemaphoreCreateMutex();
    if (!s_send_lock || !s_fifo_lock) {
        ESP_LOGE(TAG, "no memory for local-path locks");
        return;
    }
    /* Stack placement follows the SDK's own pattern (voice.c): the
     * net task writes NVS on set_volume, so its stack stays INTERNAL
     * (a PSRAM stack must never touch flash). Mic/speaker tasks only
     * move audio -- PSRAM stacks, like voice_task / the player task. */
    if (xTaskCreate(net_task, "sat_net", 4096, NULL, 4, NULL) != pdPASS)
        ESP_LOGE(TAG, "failed to start sat_net");
    if (xTaskCreateWithCaps(mic_task, "sat_mic", 4096, NULL, 6, NULL,
                            MALLOC_CAP_SPIRAM) != pdPASS)
        ESP_LOGE(TAG, "failed to start sat_mic");
    if (xTaskCreateWithCaps(speaker_task, "sat_spk", 4096, NULL, 5, NULL,
                            MALLOC_CAP_SPIRAM) != pdPASS)
        ESP_LOGE(TAG, "failed to start sat_spk");
    if (xTaskCreate(callmon_task, "sat_callmon", 2048, NULL, 3, NULL) != pdPASS)
        ESP_LOGE(TAG, "failed to start sat_callmon");
    ESP_LOGI(TAG, "local path up: Fedora service %s:%d, device %s",
             CONFIG_LYRA_SAT_SERVER_HOST, CONFIG_LYRA_SAT_SERVER_PORT,
             CONFIG_LYRA_SAT_DEVICE_ID);
}
