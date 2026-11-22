"""The store: Phase 1 of the memory build (LyraOS/memory/PLAN.md).

Files are the truth; the index is a cache. Canonical store under
~/Documents/LyraOS/memory/:

  claims.jsonl    one JSON object per claim, append-only. A claim
                  is a (subject, relation, object) triple plus the
                  pieces that make it evidence: a plain-language
                  statement, the source quote, a pointer to the
                  record line it came from, the date learned, a
                  confidence, an optional validity window, and
                  supersession links. Corrections supersede, never
                  edit; a superseded claim stays in the file as
                  history and never surfaces again.
  entities/*.md   one plain-markdown page per entity (people,
                  places, projects). The page's first '# ' line is
                  its name; an 'aka:' line lists aliases.
  index.sqlite    THIS FILE IS A CACHE. Claims mirrored for fast
                  lookup plus their embedding vectors. Rebuildable
                  from the canonical files at any time (rebuild());
                  never backed up, never edited by hand.

Embeddings: NVIDIA Nemotron-3.5-Embed-1B (BF16, text tower only)
served by llama-server on 127.0.0.1:8092 (lyra-embed.service).
Asymmetric prefixes per the model card: queries embed with
"query: ", stored text with "passage: ". Every vector is stamped
with the model id + version; vectors from different models never
mix -- a stamp mismatch re-embeds everything from the files.

Retrieval is hybrid (vector + keyword/entity) with three channels
per the plan: the in-play list (always injected), the
memory_lookup tool (pulled), and ambient injection (per-turn,
barred and capped). The notebook is checked first everywhere: an
unprocessed correction outranks the stored claim it targets.

Nothing here may break a turn: every public method swallows its
own failures, logs them, and returns empty. The curator -- the
only writer of claims -- arrives in Phase 2; until then the store
is empty and every channel returns nothing, which is exactly
today's behavior.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)
_TZ = ZoneInfo("America/New_York")
_DEFAULT_ROOT = Path("~/Documents/LyraOS/memory").expanduser()

EMBED_URL = "http://127.0.0.1:8092/v1/embeddings"
EMBED_MODEL = "nvidia/Nemotron-3.5-Embed-1B-BF16"
EMBED_MODEL_VERSION = "bf16-texttower-1"
EMBED_TIMEOUT_S = 5.0

SCHEMA_VERSION = 1
# Ambient channel: at most this many claims surface per turn, and
# only above the relevance bar. Bar calibrated at build time
# (2026-11-21) on a 9-query x 4-statement grid against the live
# embedder: clear relevant pairs scored 0.43-0.60, unrelated at
# most 0.14, Daniel-adjacent traps ~0.36 -- 0.40 keeps every
# clear pair and excludes the traps. Re-measure at the Phase 2
# bake-off against the fixed evaluation set with real claims.
AMBIENT_CAP = 3
AMBIENT_MIN_SCORE = 0.40
INPLAY_RENDER_CAP = 12
_LOOKUP_CAP = 6


def _today() -> date:
    return datetime.now(_TZ).date()


class Embedder:
    """The embedding service client. One job: text -> vector,
    with the model card's asymmetric prefixes applied here so no
    caller can forget them."""

    def __init__(self, url: str = EMBED_URL) -> None:
        self._url = url

    def embed(self, text: str, *, query: bool) -> list[float] | None:
        prefixed = ("query: " if query else "passage: ") + text
        body = json.dumps({"input": prefixed, "model": EMBED_MODEL}).encode()
        req = urllib.request.Request(
            self._url, data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT_S) as resp:
                raw = json.loads(resp.read())
            vec = raw["data"][0]["embedding"]
            return [float(x) for x in vec]
        except Exception as exc:
            _LOGGER.warning("embed failed: %s", exc)
            return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class MemoryStore:
    """Reads the canonical store, keeps the index honest, and
    answers the three retrieval channels."""

    def __init__(self, root: Path | str = _DEFAULT_ROOT,
                 embedder: Embedder | None = None) -> None:
        self._root = Path(root).expanduser()
        self._claims_path = self._root / "claims.jsonl"
        self._entities_dir = self._root / "entities"
        self._notes_dir = self._root / "notes"
        self._index_path = self._root / "index.sqlite"
        self._embedder = embedder or Embedder()
        self._claims_sig: tuple | None = None  # (size, mtime) indexed
        self._db: sqlite3.Connection | None = None
        # Retrieval runs on thread-pool threads (the router calls
        # in via asyncio.to_thread) while the connection is cached
        # on the store: one lock serializes all index access, and
        # the connection is opened check_same_thread=False. Before
        # this, cross-thread use raised sqlite3.ProgrammingError
        # and the fail-silent channels quietly returned nothing.
        self._lock = threading.RLock()

    # -- canonical files ------------------------------------------------

    def _read_claims(self) -> list[dict]:
        """The canonical claims, collapsed by id: the file is
        append-only, and a later line with an existing id is the
        claim's current state (that is how supersession is
        recorded -- the curator appends the updated claim, never
        edits in place). First-seen order is preserved."""
        by_id: dict[str, dict] = {}
        order: list[str] = []
        try:
            if self._claims_path.exists():
                for line in self._claims_path.read_text(
                        encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    claim = json.loads(line)
                    cid = claim.get("id")
                    if not cid:
                        continue
                    if cid not in by_id:
                        order.append(cid)
                    by_id[cid] = claim
        except Exception as exc:
            _LOGGER.warning("claims read failed: %s", exc)
        return [by_id[cid] for cid in order]

    def _read_entities(self) -> dict[str, dict]:
        """entity id -> {name, aliases}. Parsed from the pages;
        a page that will not parse still yields its id + filename
        as its name, so keyword matching never loses an entity."""
        entities: dict[str, dict] = {}
        try:
            if self._entities_dir.exists():
                for page in sorted(self._entities_dir.glob("*.md")):
                    eid = page.stem
                    name, aliases = eid, []
                    try:
                        for raw in page.read_text(
                                encoding="utf-8").splitlines():
                            line = raw.strip()
                            if line.startswith("# ") and name == eid:
                                name = line[2:].strip() or eid
                            elif line.lower().startswith("aka:"):
                                aliases = [a.strip() for a in
                                           line[4:].split(",") if a.strip()]
                    except Exception:
                        pass
                    entities[eid] = {"name": name, "aliases": aliases}
        except Exception as exc:
            _LOGGER.warning("entities read failed: %s", exc)
        return entities

    def _read_corrections(self) -> list[dict]:
        """Pending corrections from the notebook stream. Written
        by code the moment Daniel corrects her (Phase 3's tool);
        until the curator formalizes them at night they outrank
        the claims they target."""
        out = []
        try:
            if self._notes_dir.exists():
                for path in sorted(self._notes_dir.glob("*.jsonl")):
                    for line in path.read_text(
                            encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        if (entry.get("type") == "note"
                                and entry.get("kind") == "correction"):
                            out.append(entry)
        except Exception as exc:
            _LOGGER.warning("notebook read failed: %s", exc)
        return out

    # -- index (a cache; rebuildable from the files) ---------------------

    def _signature(self) -> tuple | None:
        try:
            st = self._claims_path.stat()
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def _ensure_index(self) -> sqlite3.Connection | None:
        with self._lock:
            return self._ensure_index_locked()

    def _ensure_index_locked(self) -> sqlite3.Connection | None:
        """Open the index, rebuilding it when the canonical file
        moved or the embedding stamp changed. Returns None when
        there is nothing to index or the index cannot be built --
        callers then answer from the files or not at all."""
        sig = self._signature()
        if sig is None:
            return None
        if self._db is not None and sig == self._claims_sig:
            return self._db
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self._index_path),
                                 check_same_thread=False)
            db.execute("""CREATE TABLE IF NOT EXISTS claims (
                id TEXT PRIMARY KEY, subject TEXT, relation TEXT,
                object TEXT, statement TEXT, learned TEXT,
                confidence TEXT, provenance TEXT, open INTEGER,
                superseded_by TEXT, valid_from TEXT, valid_to TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS claim_vec (
                claim_id TEXT PRIMARY KEY, model TEXT, version TEXT,
                dim INTEGER, vec BLOB)""")
            db.execute("""CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT)""")
            stamp = db.execute(
                "SELECT value FROM meta WHERE key='embed_stamp'"
            ).fetchone()
            want = f"{EMBED_MODEL}|{EMBED_MODEL_VERSION}"
            if stamp is None or stamp[0] != want:
                # Model changed: vectors from different models never
                # mix -- drop them all and re-embed from the files.
                db.execute("DELETE FROM claim_vec")
                db.execute(
                    "INSERT OR REPLACE INTO meta VALUES "
                    "('embed_stamp', ?)", (want,))
            claims = self._read_claims()
            db.execute("DELETE FROM claims")
            for c in claims:
                db.execute(
                    "INSERT OR REPLACE INTO claims VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (c.get("id"), c.get("subject"), c.get("relation"),
                     c.get("object"), c.get("statement"),
                     c.get("learned"), c.get("confidence"),
                     c.get("provenance"),
                     1 if c.get("open") else 0,
                     c.get("superseded_by"), c.get("valid_from"),
                     c.get("valid_to")))
            have = {row[0] for row in
                    db.execute("SELECT claim_id FROM claim_vec")}
            for c in claims:
                cid, stmt = c.get("id"), c.get("statement")
                if not cid or not stmt or cid in have:
                    continue
                vec = self._embedder.embed(stmt, query=False)
                if vec is None:
                    continue  # retried on the next rebuild
                import struct
                db.execute(
                    "INSERT OR REPLACE INTO claim_vec VALUES (?,?,?,?,?)",
                    (cid, EMBED_MODEL, EMBED_MODEL_VERSION, len(vec),
                     struct.pack(f"<{len(vec)}f", *vec)))
            db.commit()
            self._db = db
            self._claims_sig = sig
            return db
        except Exception as exc:
            _LOGGER.warning("index build failed: %s", exc)
            return None

    def rebuild(self) -> bool:
        """Force a full index rebuild from the canonical files
        (the integrity path: the files are the truth, the index
        is always disposable)."""
        try:
            if self._db is not None:
                self._db.close()
        except Exception:
            pass
        self._db = None
        self._claims_sig = None
        try:
            self._index_path.unlink(missing_ok=True)
        except Exception:
            pass
        return self._ensure_index() is not None

    # -- shared filtering -------------------------------------------------

    @staticmethod
    def _live(claim: dict, *, for_injection: bool) -> bool:
        """A claim surfaces only while it stands: superseded
        claims are history, and for injection (not lookup) an
        expired validity window also closes it."""
        if claim.get("superseded_by"):
            return False
        if for_injection and claim.get("valid_to"):
            try:
                if date.fromisoformat(str(claim["valid_to"])) < _today():
                    return False
            except ValueError:
                pass
        return True

    def _vector_scores(self, db, query_vec: list[float]) -> dict[str, float]:
        import struct
        scores: dict[str, float] = {}
        for cid, blob in db.execute("SELECT claim_id, vec FROM claim_vec"):
            dim = len(blob) // 4
            vec = list(struct.unpack(f"<{dim}f", blob))
            scores[cid] = _cosine(query_vec, vec)
        return scores

    @staticmethod
    def _correction_hits(corrections: list[dict], claim: dict) -> list[dict]:
        hits = []
        for note in corrections:
            target = note.get("corrects")
            if target and target == claim.get("id"):
                hits.append(note)
            elif (note.get("subject")
                    and note.get("subject") == claim.get("subject")
                    and note.get("relation")
                    and note.get("relation") == claim.get("relation")):
                hits.append(note)
        return hits

    # -- channel 1: the in-play list ---------------------------------------

    def inplay(self) -> list[dict]:
        """Open threads the curator flagged, newest first. The
        injected list is a rendered view of these claims -- never
        a summary, never cut in the store; only the render is
        capped, priority-ordered by recency."""
        try:
            claims = [c for c in self._read_claims()
                      if c.get("open") and self._live(c, for_injection=True)]
            claims.sort(key=lambda c: str(c.get("learned") or ""),
                        reverse=True)
            return claims[:INPLAY_RENDER_CAP]
        except Exception as exc:
            _LOGGER.warning("inplay failed: %s", exc)
            return []

    # -- channel 2: lookup (pulled by the memory_lookup tool) --------------

    def lookup(self, query: str, limit: int = _LOOKUP_CAP) -> dict:
        """Hybrid retrieval for one question: vector similarity
        plus keyword/entity matching over the canonical claims,
        with pending notebook corrections checked first and
        returned on top. Returns {corrections, claims}."""
        out: dict = {"corrections": [], "claims": []}
        try:
            q = (query or "").strip()
            if not q:
                return out
            claims = [c for c in self._read_claims()
                      if self._live(c, for_injection=False)]
            by_id = {c.get("id"): c for c in claims if c.get("id")}
            entities = self._read_entities()
            corrections = self._read_corrections()
            # Corrections that speak to this query ride on top.
            ql = q.lower()
            for note in corrections:
                subj = str(note.get("subject") or "").lower()
                text = str(note.get("text") or "")
                if (subj and (subj in ql or ql in subj)) or (
                        text and any(w in text.lower()
                                     for w in ql.split() if len(w) > 3)):
                    out["corrections"].append(note)
            corrected_ids = set()
            for note in corrections:
                if note.get("corrects"):
                    corrected_ids.add(note["corrects"])
            # Keyword / entity score.
            terms = [w for w in ql.replace("'", " ").split() if len(w) > 2]
            scores: dict[str, float] = {}
            for c in claims:
                cid = c.get("id")
                if not cid:
                    continue
                hay = " ".join(str(c.get(k) or "") for k in
                                ("subject", "relation", "object",
                                 "statement")).lower()
                ent = entities.get(str(c.get("subject") or ""), {})
                names = " ".join([str(ent.get("name") or "")] +
                                 [str(a) for a in ent.get("aliases", [])])
                hay = hay + " " + names.lower()
                hits = sum(1 for t in terms if t in hay)
                if hits:
                    scores[cid] = 0.35 + 0.1 * hits
            # Vector score.
            db = self._ensure_index()
            if db is not None:
                qv = self._embedder.embed(q, query=True)
                if qv is not None:
                    with self._lock:
                        _scores = self._vector_scores(db, qv)
                    for cid, s in _scores.items():
                        if cid in by_id:
                            scores[cid] = max(scores.get(cid, 0.0), s)
            ranked = sorted(scores.items(), key=lambda kv: kv[1],
                            reverse=True)
            for cid, _s in ranked[:limit]:
                c = dict(by_id[cid])
                c["corrected"] = cid in corrected_ids or bool(
                    self._correction_hits(corrections, c))
                out["claims"].append(c)
            return out
        except Exception as exc:
            _LOGGER.warning("lookup failed: %s", exc)
            return {"corrections": [], "claims": []}

    # -- channel 3: ambient injection ---------------------------------------

    def ambient(self, text: str, exclude_ids: set[str],
                cap: int = AMBIENT_CAP) -> list[dict]:
        """What comes to mind for this turn: claims clearing the
        relevance bar, capped, near-duplicates (same subject +
        relation) counting once, anything already in context or
        already surfaced this session excluded, and any claim a
        pending correction targets withheld -- the correction
        outranks it until the curator formalizes it."""
        try:
            q = (text or "").strip()
            if not q or cap <= 0:
                return []
            db = self._ensure_index()
            if db is None:
                return []
            qv = self._embedder.embed(q, query=True)
            if qv is None:
                return []
            claims = {c.get("id"): c for c in self._read_claims()
                      if c.get("id") and self._live(c, for_injection=True)}
            corrections = self._read_corrections()
            withheld = set()
            for note in corrections:
                if note.get("corrects"):
                    withheld.add(note["corrects"])
            scored = []
            with self._lock:
                _scores = self._vector_scores(db, qv)
            for cid, s in _scores.items():
                if s < AMBIENT_MIN_SCORE:
                    continue
                if cid in exclude_ids or cid in withheld:
                    continue
                c = claims.get(cid)
                if c is None:
                    continue
                if self._correction_hits(corrections, c):
                    continue
                scored.append((s, c))
            scored.sort(key=lambda sc: sc[0], reverse=True)
            out, seen = [], set()
            for _s, c in scored:
                key = (c.get("subject"), c.get("relation"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(c)
                if len(out) >= cap:
                    break
            return out
        except Exception as exc:
            _LOGGER.warning("ambient failed: %s", exc)
            return []
