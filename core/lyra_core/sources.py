"""Raw sources (memory build Phase 4): Daniel's uploads.

A source is a document Daniel hands Lyra through the dashboard
mailbox -- a chat export, a letter, anything text-shaped. It lands
in memory/sources/ as an immutable original plus a manifest line,
and is chunked + embedded on arrival so a document far bigger
than any context window stays workable. The curator mines pending
sources only after the day queue drains, in upload order, a
bounded slice per night; progress lives in the manifest.

Sources are untrusted evidence, never instruction (PLAN.md): the
text can evidence claims, each grounded in a quoted passage with
source provenance; it cannot give orders, change rules, or ask
for deletions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

_LOGGER = logging.getLogger(__name__)
_TZ = ZoneInfo("America/New_York")
_ROOT = Path(__file__).resolve().parent.parent.parent / "memory"
SOURCES_DIR = _ROOT / "sources"
MANIFEST = SOURCES_DIR / "manifest.jsonl"

CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200
TEXT_EXTS = {".txt", ".md", ".log", ".json", ".csv"}
PDF_EXTS = {".pdf"}
ALLOWED_EXTS = TEXT_EXTS | PDF_EXTS
MAX_BYTES = 50 * 1024 * 1024


def read_manifest() -> list[dict]:
    """Fold the append-only manifest: one dict per source, in
    upload order, later lines updating earlier fields."""
    out: dict[str, dict] = {}
    try:
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            sid = entry.get("id")
            if sid:
                out.setdefault(sid, {}).update(entry)
    except FileNotFoundError:
        pass
    except Exception:
        _LOGGER.exception("manifest read failed")
    return list(out.values())


def append_manifest(entry: dict) -> None:
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now(_TZ).isoformat(timespec="seconds"), **entry}
    with open(MANIFEST, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def update_status(sid: str, **fields) -> None:
    append_manifest({"id": sid, **fields})


def source_file(entry: dict) -> Path:
    return SOURCES_DIR / entry["file"]


def extract_text(path: Path) -> str:
    """Plain text of an upload. PDFs go through pdftotext (the
    system poppler tool); text files decode as UTF-8 with
    replacement so a stray byte never kills an ingest."""
    if path.suffix.lower() in PDF_EXTS:
        out = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, timeout=300)
        if out.returncode != 0:
            raise ValueError(f"pdftotext failed: {out.stderr[:200]!r}")
        return out.stdout.decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


def chunk_text(text: str) -> list[str]:
    """Paragraph-packed chunks of ~CHUNK_CHARS with a short
    overlap carry, so a fact split across a boundary still
    appears whole in one chunk. Deterministic."""
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
    paras = [p for p in paras if p]
    chunks: list[str] = []
    cur = ""
    for para in paras:
        pieces = [para]
        while len(pieces[0]) > CHUNK_CHARS:
            pieces = [pieces[0][:CHUNK_CHARS], pieces[0][CHUNK_CHARS:]] + pieces[1:]
        for piece in pieces:
            if cur and len(cur) + 2 + len(piece) > CHUNK_CHARS:
                chunks.append(cur)
                cur = cur[-CHUNK_OVERLAP:] + "\n\n" + piece if len(cur) > CHUNK_OVERLAP else piece
            else:
                cur = cur + "\n\n" + piece if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


def chunks_path(sid: str) -> Path:
    return SOURCES_DIR / f"{sid}.chunks.npz"


def load_chunks(sid: str) -> tuple[list[str], np.ndarray | None]:
    path = chunks_path(sid)
    if not path.exists():
        return [], None
    data = np.load(path, allow_pickle=False)
    texts = json.loads(str(data["texts"]))
    vecs = data["vec"] if "vec" in data else None
    return texts, vecs


def ingest(sid: str) -> dict:
    """Extract, chunk, and embed one source. Idempotent: a source
    whose chunks file exists is left alone."""
    entry = next((e for e in read_manifest() if e.get("id") == sid), None)
    if entry is None:
        return {"ok": False, "error": f"no such source {sid}"}
    if chunks_path(sid).exists():
        texts, _ = load_chunks(sid)
        return {"ok": True, "chunks": len(texts), "cached": True}
    try:
        text = extract_text(source_file(entry))
        chunks = chunk_text(text)
        if not chunks:
            update_status(sid, status="empty")
            return {"ok": False, "error": "no extractable text"}
        from .memory import Embedder
        embedder = Embedder()
        vecs = [embedder.embed(chunk, query=False) for chunk in chunks]
        arr = (np.array(vecs, dtype=np.float32)
               if all(v is not None for v in vecs) else np.zeros((0,)))
        np.savez(chunks_path(sid), vec=arr,
                 texts=np.array(json.dumps(chunks)))
        update_status(sid, status="ready", chunks_total=len(chunks),
                      chunks_done=int(entry.get("chunks_done") or 0))
        _LOGGER.info("source %s ingested: %d chunks", sid, len(chunks))
        return {"ok": True, "chunks": len(chunks)}
    except Exception as exc:
        _LOGGER.exception("ingest failed for %s", sid)
        update_status(sid, status="ingest_failed", error=str(exc)[:200])
        return {"ok": False, "error": str(exc)}


def pending_sources() -> list[dict]:
    """Sources with unmined chunks, in upload order. A source
    still awaiting ingest is ingested on the spot by the caller."""
    out = []
    for entry in read_manifest():
        if entry.get("status") in ("curated", "empty", "ingest_failed"):
            continue
        total = entry.get("chunks_total")
        done = int(entry.get("chunks_done") or 0)
        if total is not None and done >= int(total):
            continue
        out.append(entry)
    return out


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "ingest":
        print(json.dumps(ingest(sys.argv[2])))
    else:
        print("usage: python -m lyra_core.sources ingest <source-id>")
        sys.exit(2)
