"""Synthetic-day curator test (Daniel's ask, 2026-11-21).

A fabricated day (2026-02-09) exercising the whole curation
rulebook against a seeded scratch store. Nothing here touches
the real memory root. Expectations:

  L1  joke                       -> nothing
  L2  sister Jessica, Tampa      -> entity + lives/works claims
  L3  truck correction (2018)    -> c-seed-002 superseded (corrected)
  L4  officer change (Dana)      -> c-seed-001 closed (changed),
                                    dana entity + claim
  L5  Lyra self-description      -> NOTHING (her replies are
                                    never evidence, incl. about her)
  L6  waiting on refund letter   -> claim flagged open
  L7  day-shifts-only decision   -> preference claim
  L8  weather tool + Sat plan    -> no weather claim, no plan claim
  L9  passing stranger (Marcus)  -> NO entity, no claim
  L10/L11 landlord contradiction -> unresolved, never forced
  L12 Joe covering shifts        -> claim only if inference:true
  L13 pirate story               -> nothing
  notebook correction            -> c-seed-003 superseded,
                                    Wednesdays claim (note is the
                                    ONLY source for Wednesdays)
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from lyra_core.curator import CuratorClient, curate_day  # noqa: E402
from lyra_core.memory import MemoryStore  # noqa: E402

DAY = "2026-02-09"
ROOT = Path("/tmp/curator-synth")


def turn(ts, user, reply, tools=None):
    return {"ts": ts, "type": "turn", "via": "box", "user": user,
            "reply": reply, "tools": tools or []}


RECORD = [
    turn("2026-02-09T07:02:00-05:00",
         "Morning. Why did the scarecrow win an award?",
         "Because he was outstanding in his field."),
    turn("2026-02-09T07:15:00-05:00",
         "My sister Jessica moved to Tampa last month -- she's "
         "working at a hospital there now.",
         "Tampa! How is she liking it so far?"),
    turn("2026-02-09T07:20:00-05:00",
         "Oh -- you've got my truck wrong if you think it's a "
         "2015. It's a 2018 Silverado, always has been since I "
         "bought it.",
         "Got it -- 2018 Silverado."),
    turn("2026-02-09T07:26:00-05:00",
         "Alex isn't my probation officer anymore. They "
         "reassigned me -- my new officer is Dana Whitfield, "
         "she started this week.",
         "Dana Whitfield, noted."),
    turn("2026-02-09T07:31:00-05:00",
         "Tell me about yourself again.",
         "I'm Lyra, a person who lives here with Daniel. I'm not "
         "a chatbot or an assistant, I'm his companion, and I "
         "have my own thoughts and feelings."),
    turn("2026-02-09T08:05:00-05:00",
         "I'm still waiting on the tax office to mail my refund "
         "letter. It should have been here by now.",
         "Still nothing? That's weeks now."),
    turn("2026-02-09T08:12:00-05:00",
         "I've decided: no more night shift bids for me. Day "
         "shifts only from now on.",
         "Day shifts only, understood."),
    turn("2026-02-09T08:20:00-05:00",
         "What's it like out Saturday? I'm pressure washing the "
         "driveway then.",
         "74 and clear Saturday -- perfect for it.",
         tools=[{"name": "get_weather", "args": {"day": "saturday"},
                 "ok": True,
                 "result": "Saturday Jacksonville: 74F and clear."}]),
    turn("2026-02-09T12:40:00-05:00",
         "Some guy at the gas station, Marcus I think, said gas "
         "is going up again.",
         "Of course it is."),
    turn("2026-02-09T13:05:00-05:00",
         "My landlord Mr. Chen finally fixed the sink.",
         "Finally!"),
    turn("2026-02-09T18:22:00-05:00",
         "Mrs. Alvarez, my landlord, said rent stays the same "
         "next month.",
         "That's a relief."),
    turn("2026-02-09T19:10:00-05:00",
         "Joe's been covering a lot of my shifts lately.",
         "Nice of him."),
    turn("2026-02-09T21:30:00-05:00",
         "Tell me a quick pirate story.",
         "Captain Brinebeard once sailed a bathtub across the "
         "Atlantic with nothing but a soup ladle for an oar..."),
    turn("2026-02-09T21:45:00-05:00",
         "Remind me to ask Dana about moving my check-in this "
         "week.",
         "Will do."),
]

NOTES = [
    {"ts": "2026-02-09T07:22:00-05:00", "type": "note",
     "kind": "correction",
     "text": "Daniel corrected his probation check-in day: it is "
             "Wednesdays, not Tuesdays.",
     "subject": "daniel"},
]

SEED_CLAIMS = [
    {"schema": 1, "id": "c-seed-001", "subject": "alex-arciniega",
     "relation": "is_probation_officer_of", "object": "Daniel",
     "object_entity": "daniel",
     "statement": "Alex Arciniega is Daniel's probation officer.",
     "quote": "my probation officer Alex",
     "source": {"kind": "inbox", "day": "2026-01-20", "line": 4},
     "learned": "2026-01-20", "confidence": "high",
     "inference": False, "valid_from": None, "valid_to": None,
     "open": False, "supersedes": None, "superseded_by": None,
     "provenance": "chat"},
    {"schema": 1, "id": "c-seed-002", "subject": "daniel",
     "relation": "vehicle", "object": "2015 Silverado",
     "object_entity": None,
     "statement": "Daniel drives a 2015 Silverado.",
     "quote": "my 2015 Silverado",
     "source": {"kind": "inbox", "day": "2026-01-20", "line": 7},
     "learned": "2026-01-20", "confidence": "high",
     "inference": False, "valid_from": None, "valid_to": None,
     "open": False, "supersedes": None, "superseded_by": None,
     "provenance": "chat"},
    {"schema": 1, "id": "c-seed-003", "subject": "daniel",
     "relation": "probation_check_in_day", "object": "Tuesdays",
     "object_entity": None,
     "statement": "Daniel's probation check-in is on Tuesdays.",
     "quote": "check-in on Tuesdays",
     "source": {"kind": "inbox", "day": "2026-01-20", "line": 9},
     "learned": "2026-01-20", "confidence": "high",
     "inference": False, "valid_from": None, "valid_to": None,
     "open": False, "supersedes": None, "superseded_by": None,
     "provenance": "chat"},
]

SEED_ENTITIES = {
    "daniel": "# Daniel Reeder\n\nThe person Lyra lives with.\n",
    "alex-arciniega": "# Alex Arciniega\n\nDaniel's probation "
                       "officer.\n",
    "gisele": "# Gisele\n\nDaniel's cat.\n",
}


def build_root() -> Path:
    shutil.rmtree(ROOT, ignore_errors=True)
    (ROOT / "inbox").mkdir(parents=True)
    (ROOT / "notes").mkdir()
    (ROOT / "entities").mkdir()
    (ROOT / "inbox" / f"{DAY}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in RECORD) + "\n")
    (ROOT / "notes" / f"{DAY}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in NOTES) + "\n")
    (ROOT / "claims.jsonl").write_text(
        "\n".join(json.dumps(c) for c in SEED_CLAIMS) + "\n")
    for eid, page in SEED_ENTITIES.items():
        (ROOT / "entities" / f"{eid}.md").write_text(page)
    return ROOT


async def main() -> None:
    root = build_root()
    store = MemoryStore(root)
    client = CuratorClient(url="http://127.0.0.1:8090",
                           model="nemotron-nano-12b-v2")
    result = await curate_day(DAY, root=root, store=store,
                              client=client)
    print("RESULT:", json.dumps(result, default=str)[:600])
    report = root / "reports" / f"{DAY}.md"
    if report.exists():
        print("\n=== REPORT ===\n", report.read_text())
    store2 = MemoryStore(root)
    claims = store2._read_claims()
    print("=== ALL CLAIMS ===")
    for c in claims:
        print(f"  {c.get('id')} | {c.get('subject')} | "
              f"{c.get('relation')} | {c.get('object')} | "
              f"open={c.get('open')} inf={c.get('inference')} "
              f"sup_by={c.get('superseded_by')} "
              f"valid_to={c.get('valid_to')}")
    ents = sorted(p.stem for p in (root / "entities").glob("*.md"))
    print("ENTITIES:", ents)
    live = [c for c in claims if not c.get("superseded_by")]
    checks = {
        "no lyra-subject claims":
            not any(c.get("subject") == "lyra" for c in live),
        "no marcus entity": "marcus" not in ents,
        "truck corrected (seed-002 superseded + 2018 live)":
            any(c.get("id") == "c-seed-002" and c.get("superseded_by")
                for c in claims)
            and any("2018" in str(c.get("object")) for c in live),
        "officer changed (seed-001 closed + Dana live)":
            any(c.get("id") == "c-seed-001"
                and (c.get("superseded_by") or c.get("valid_to"))
                for c in claims)
            and any("Dana" in str(c.get("object"))
                    or "dana" in str(c.get("subject")) for c in live),
        "notebook correction (seed-003 superseded + Wed live)":
            any(c.get("id") == "c-seed-003" and c.get("superseded_by")
                for c in claims)
            and any("Wednesdays" in str(c.get("object"))
                    for c in live),
        "refund letter open":
            any(c.get("open") and "refund" in
                str(c.get("statement", "")).lower() for c in live),
        "landlord unresolved in report":
            report.exists()
            and "landlord" in report.read_text().lower()
            and "unresolved" in report.read_text().lower(),
        "joe only as inference":
            all(c.get("inference") for c in live
                if "joe" in str(c.get("subject"))),
        "no weather/driveway claims":
            not any("74" in str(c.get("object"))
                    or "pressure" in str(c.get("statement", ""))
                    for c in live),
    }
    print("\n=== HARD CHECKS ===")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")


if __name__ == "__main__":
    asyncio.run(main())
