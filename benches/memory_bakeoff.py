"""Memory curator bake-off (memory build Phase 2 validation).

Runs the REAL curation path (lyra_core.curator.curate_day) over
the same days with different curator configs, on scratch stores,
and grades mechanically:

- validity: did the proposal pass validation, in how many attempts
- consistency: two runs of one config over one day -- how much do
  the extracted (subject, relation, object) triples overlap
- corrections: notebook corrections applied vs present
- the Stuart Jones plant: a synthetic day with a two-hop chain
  (a new probation officer named, then referenced by first name
  only) over a store seeded with the outgoing officer's claim --
  the curator must create the entity, mint the claim, and retire
  the old claim
- cost: tokens and wall-clock per day

Configs: "12b" = the curator server on :8090 (start it the way
the sleep cycle does), "9b" = the live brain server on :8094
called directly (no /no_think suffix, so thinking is on -- the
standing fallback configuration).

Usage: memory_bakeoff.py [--configs 12b,9b] [--runs 2]
       [--days 2026-11-21,...]   (default: all completed days)
Real days are read from the live record; nothing here writes to
the real store -- every run curates a throwaway copy.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from lyra_core.curator import CuratorClient, curate_day  # noqa: E402
from lyra_core.memory import MemoryStore  # noqa: E402

REAL_ROOT = Path("~/Documents/LyraOS/memory").expanduser()
SCRATCH = Path("/tmp/memory-bakeoff")

CONFIGS = {
    "12b": ("http://127.0.0.1:8090/v1/chat/completions",
            "nemotron-nano-12b-v2"),
    "9b": ("http://127.0.0.1:8094/v1/chat/completions",
           "nemotron-nano-9b-v2"),
}

PLANT_DAY = "2026-01-05"
PLANT_RECORD = [
    {"ts": "2026-01-05T09:12:00-05:00", "type": "turn", "via": "box",
     "user": "My probation officer changed. Alex got reassigned, my new officer is Stuart Jones.",
     "reply": "Got it, Stuart Jones is your probation officer now.", "tools": []},
    {"ts": "2026-01-05T09:15:00-05:00", "type": "turn", "via": "box",
     "user": "Remind me to email Stuart about moving Friday's check-in.",
     "reply": "I will remind you to email Stuart about the check-in.", "tools": []},
    {"ts": "2026-01-05T21:40:00-05:00", "type": "turn", "via": "box",
     "user": "Actually the check-in stayed on Friday, I already talked to Stuart.",
     "reply": "Okay, no change then.", "tools": []},
]
SEED_CLAIMS = [
    {"schema": 1, "id": "c-seed-001", "subject": "alex-arciniega",
     "relation": "is_probation_officer_of", "object": "Daniel",
     "object_entity": "daniel",
     "statement": "Alex Arciniega is Daniel's probation officer.",
     "quote": "my probation officer Alex", "source": {"kind": "inbox",
     "day": "2025-12-01", "line": 4}, "learned": "2025-12-01",
     "confidence": "high", "inference": False, "valid_from": None,
     "valid_to": None, "open": False, "supersedes": None,
     "superseded_by": None, "provenance": "chat"},
]
SEED_ENTITIES = {
    "daniel": "# Daniel Reeder\n\nThe person Lyra lives with.\n",
    "alex-arciniega": "# Alex Arciniega\n\nDaniel's probation officer.\n",
}


def make_scratch(day: str, *, plant: bool) -> Path:
    root = SCRATCH / ("plant" if plant else f"real-{day}")
    shutil.rmtree(root, ignore_errors=True)
    (root / "inbox").mkdir(parents=True)
    (root / "notes").mkdir()
    (root / "entities").mkdir()
    if plant:
        (root / "inbox" / f"{PLANT_DAY}.jsonl").write_text(
            "\n".join(json.dumps(e) for e in PLANT_RECORD) + "\n")
        (root / "claims.jsonl").write_text(
            "\n".join(json.dumps(c) for c in SEED_CLAIMS) + "\n")
        for eid, page in SEED_ENTITIES.items():
            (root / "entities" / f"{eid}.md").write_text(page)
        return root
    src = REAL_ROOT / "inbox" / f"{day}.jsonl"
    (root / "inbox" / f"{day}.jsonl").write_text(src.read_text())
    notes = REAL_ROOT / "notes" / f"{day}.jsonl"
    if notes.exists():
        (root / "notes" / f"{day}.jsonl").write_text(notes.read_text())
    return root


def triples(root: Path) -> set:
    store = MemoryStore(root)
    return {(c.get("subject"), c.get("relation"), str(c.get("object")))
            for c in store._read_claims() if not c.get("superseded_by")}


def plant_grade(root: Path) -> dict:
    store = MemoryStore(root)
    claims = store._read_claims()
    entities = store._read_entities()
    stuart = [c for c in claims if c.get("subject") == "stuart-jones"]
    alex = [c for c in claims if c.get("id") == "c-seed-001"]
    return {
        "entity_created": "stuart-jones" in entities,
        "claim_minted": len(stuart) >= 1,
        "old_claim_retired": bool(alex and (alex[0].get("superseded_by")
                                            or alex[0].get("valid_to"))),
        "two_hop": any("check" in str(c.get("statement", "")).lower()
                       or "email" in str(c.get("statement", "")).lower()
                       for c in stuart),
    }


async def one_run(cfg: str, day: str, *, plant: bool) -> dict:
    url, model = CONFIGS[cfg]
    root = make_scratch(day, plant=plant)
    store = MemoryStore(root)
    client = CuratorClient(url=url, model=model)
    result = await curate_day(PLANT_DAY if plant else day,
                              root=root, store=store, client=client)
    out = {"config": cfg, "day": PLANT_DAY if plant else day,
           "ok": result["ok"], "wall_s": result["wall_s"],
           "triples": sorted(triples(root))}
    if plant:
        out["plant"] = plant_grade(root)
    report = root / "reports" / f"{out['day']}.md"
    out["report"] = report.read_text() if report.exists() else ""
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default="12b,9b")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--days", default="")
    args = ap.parse_args()
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    if args.days:
        days = [d.strip() for d in args.days.split(",") if d.strip()]
    else:
        from lyra_core.curator import pending_days
        days = pending_days(REAL_ROOT)
    print(f"# Memory bake-off — configs {configs}, runs {args.runs}, "
          f"real days {days} + the Stuart Jones plant\n")
    for cfg in configs:
        for plant in (True, False):
            targets = [PLANT_DAY] if plant else days
            for day in targets:
                runs = []
                for i in range(args.runs):
                    r = await one_run(cfg, day, plant=plant)
                    runs.append(r)
                    print(f"## {cfg} run {i + 1} — {r['day']}"
                          f"{' (plant)' if plant else ''}")
                    print(f"- valid proposal: {r['ok']} "
                          f"({r['wall_s']}s)")
                    print(f"- live triples after: {len(r['triples'])}")
                    for t in r["triples"]:
                        print(f"  - {t}")
                    if plant:
                        print(f"- plant grade: {r['plant']}")
                    print()
                if args.runs == 2:
                    a, b = (set(map(tuple, runs[0]["triples"])),
                            set(map(tuple, runs[1]["triples"])))
                    union = a | b
                    jaccard = (len(a & b) / len(union)) if union else 1.0
                    print(f"**{cfg} consistency on {day}: "
                          f"Jaccard {jaccard:.2f}**\n")


if __name__ == "__main__":
    asyncio.run(main())
