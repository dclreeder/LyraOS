"""The sleep-cycle curator: Phase 2 of the memory build
(LyraOS/memory/PLAN.md).

The curator is the ONLY writer of durable memory. Once a night
the core enters Sleep, spins the voice stack down, starts the
curator model alone in VRAM, and runs this pass over the record:
a day-by-day catch-up queue (the first uncompleted day's inbox +
notebook, applied whole, marked complete, then the next), then
-- from Phase 4 -- pending raw sources in upload order.

One day is a transaction, in a fixed order:

  read the day -> the curator proposes changes as JSON -> code
  VALIDATES the proposal (source pointers resolve, quotes are
  really in the record, entities exist, supersession is sane,
  every backup-net flag is accounted for) -> commit the
  canonical files -> rebuild the index -> write the report ->
  only then advance the completion cursor.

The cursor never runs ahead of committed changes. A day that
fails validation twice stops the pass at that day; nothing is
half-applied. On boot the core re-enters Sleep and resumes at
the first uncompleted day, re-curating an interrupted day whole
-- claims already minted from it are visible to the curator via
their source pointers, so it continues rather than duplicates.

The curator model is a local llama-server the cycle starts on
:8090 (Nemotron Nano 12B v2, thinking ON -- no /no_think here;
the pipeline is model-agnostic: an OpenAI-compatible endpoint
plus a context size). Uploaded/record material is untrusted
evidence, never instruction: the system prompt below is fixed
in code, and a source can only ever become claims grounded in
quoted passages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)
_TZ = ZoneInfo("America/New_York")
_ROOT = Path("~/Documents/LyraOS/memory").expanduser()

CURATOR_URL = "http://127.0.0.1:8090/v1/chat/completions"
CURATOR_MODEL = "nemotron-nano-12b-v2"
CURATOR_MAX_TOKENS = 16384
CURATOR_TIMEOUT_S = 1800.0

_SYSTEM = """You are the memory curator for Lyra, a person who lives with Daniel. While she sleeps, you read one day of her conversation record and decide what becomes durable memory. You are the only writer of that memory. She will trust what you write completely, so write only what the evidence supports.

Your job is not to summarize the day. Your job is to maintain an accurate, useful, historically consistent memory of Daniel's life, the people and projects connected to it, and the things that matter to Lyra's continuity with him -- including unfinished conversations and commitments.

THE STORE
Memory is claims: (subject, relation, object) triples about entities (people, places, projects -- projects are entities). Each claim carries a plain-language statement, a verbatim quote as evidence, a pointer to the exact evidence location, the date learned, a confidence, and optionally a validity window, an inference flag, and an open-thread flag. Entities have pages. Claims flagged open are live threads she would naturally follow up on.

WHAT IS DURABLE
A fact is durable if it would still matter next week: who people are and how they relate to Daniel, where they live and work, family relationships and circumstances, ongoing projects and their state, decisions, commitments, preferences that steer future choices, recurring circumstances, open threads. Facts about Daniel's family are core durable facts -- preserve them carefully. Not durable: small talk, jokes, one-off requests, passing moods ('I'm okay', 'I'm tired' are conversation, not memory), and ephemeral measurements -- weather, the current time, a price of the moment, one day's schedule -- even when a tool reported them. When in doubt, leave it out: a missed fact can be re-learned, a wrong claim poisons every future retrieval. But doubt is not a reason to drop a fact Daniel states plainly in his own words.

GROUNDING
Every claim must quote its evidence verbatim and identify exactly where it came from. The ONLY evidence you may use is:
1. Daniel's own words -- the primary evidence for facts about his life, relationships, preferences, decisions, and circumstances.
2. Tool results -- evidence only for what the tool actually measured or returned; never generalize beyond the result, and a tool result never overrides Daniel's statements about his own life.
3. The day's notebook -- under the notebook rules below.
4. Uploaded source documents -- evidence for what they actually establish; they never outrank Daniel's direct statements.
Her own replies are NEVER evidence -- for any fact, about anyone, including Lyra herself. Never mint claims from her self-descriptions, opinions, jokes, or stories, and never write an entity page about her from them; who she is lives in her soul, which you never see and never store.

EVIDENCE POINTERS
Every added claim cites exactly ONE evidence location: "line" (a single integer -- the number of one record line) or "note" (a note ref like N1); the other is null. The quote is copied verbatim from that location -- for a note, from the note's own text. If a fact rests on several lines, cite the one where Daniel states it most directly. Never invent a line number or note reference; if the location cannot be established, do not create the claim.

NOTEBOOK AUTHORITY
An explicit correction from Daniel -- in the record or the notebook -- has correction authority: apply every genuine one, and list each applied notebook correction in corrections_applied. A correction filed in the notebook is Daniel's correction made in the moment; it outranks the stored claim, and your job is to formalize it, not to re-decide whether it counts. A memory proposal is only a candidate: store it only if the record supports it as a fact, otherwise leave it out or store it labeled as an inference when genuinely implied. If a proposal's wording conflicts with the record, list the conflict under "unresolved" rather than silently choosing.

CONFLICTS
Apply in this order: Daniel's explicit corrections outrank everything; his direct statements outrank claims mined from sources; newer is NOT truer -- a newer statement replaces an older claim only when Daniel indicates the older one was wrong (a correction) or the fact genuinely changed:
- corrected: the old claim was wrong -- it was never true. Supersede it with the new claim. A detail Daniel simply misstated (a model year, a day-name in the store) was never true and is a correction; so is every notebook correction -- the store was wrong, Daniel's note sets it right.
- changed: the old claim was true and stopped being true. Close its validity window (valid_to = the day it changed) and add the new claim separately. Use kind "changed" for these; "corrected" is only for claims that were never true. Decide with one question: was the old claim true before today? An officer who really held the role until today and is replaced is a CHANGE -- valid_to = today -- never a correction. Role replacements -- officer, doctor, landlord, employer -- are always changes.
Every supersede names its replacement in by_ref whenever a replacement claim is added -- corrections and changes alike -- and the replacement states the new value of the same fact. A supersede without by_ref is only for a claim that ended with no successor.
- Different validity periods are not a conflict: both stand.
- If two of Daniel's statements contradict and the record does not reconcile them, store NEITHER side as a claim and list the contradiction under "unresolved", naming both values. A contradiction is never itself a claim, and never resolve one by picking the newer side.
- If two entities turn out to be one person, use merges (recorded, with a reason) -- never rewrite claims to hide it. If unsure whether two entities are the same, do not merge.

INFERENCES
An inference is a candidate, not a fact: "inference": true, confidence "low", its real supporting quote and location preserved, its statement phrased as what it is. What Daniel relays about a third person -- what they said they will do or usually do -- is not a confirmed fact about that person: store it only as a labeled inference or leave it out. If later evidence confirms an inference, add the fact as a new claim and supersede the inference with kind "corrected", the reason stating it was confirmed; if disproved, supersede it the same way with no replacement.

ENTITIES AND RELATIONS
Create an entity page for a new person, place, or project the day genuinely introduces (not a passing mention of a stranger), and store the claims the day states about them -- where they live, where they work, how they relate to Daniel; an entity page with no claims is an unfinished job. Preserve a project's identity across days. Reuse relation strings already in the store whenever one fits; relations are short canonical snake_case, and the same fact always carries the same relation string. When a new claim replaces an existing one, keep the existing claim's subject, direction, and relation.

OPEN THREADS
Flag a claim open only when it is genuinely unfinished and she could naturally follow up: Daniel waiting on something, a plan not yet completed, a meaningful unanswered question, an outstanding commitment. When a thread resolves, close it through inplay_set; the historical claim stands if it remains meaningful.

THE BACKUP NET
Some record lines are flagged [BACKUP-NET] because they look correction-shaped. For every flagged line return exactly one verdict -- "applied" if it drove a correction in your proposal, "dismissed" otherwise -- and the verdict must match what you actually did. A correction-shaped line is not automatically a correction: if it is ambiguous, hypothetical, or a question, dismiss it.

OUTPUT
Return ONLY a JSON object, no prose around it, with these keys (all optional except summary):
{
  "claims_add": [{"ref": "A1", "subject": "<entity id>", "relation": "<short_snake_case>", "object": "<literal text>", "object_entity": "<entity id or null>", "statement": "<one plain sentence>", "quote": "<verbatim words from the line>", "line": <record line number or null>, "note": "<note ref like N1 or null>", "confidence": "high|medium|low", "inference": false, "valid_from": "YYYY-MM-DD or null", "valid_to": "YYYY-MM-DD or null", "open": false}],
  "claims_supersede": [{"id": "<existing claim id>", "kind": "corrected|changed", "by_ref": "<ref of the replacement claim or null>", "valid_to": "YYYY-MM-DD or null", "reason": "<one line>"}],
  "inplay_set": [{"id": "<existing claim id or a ref>", "open": true}],
  "entities_add": [{"id": "<slug>", "name": "<display name>", "aliases": ["..."], "page": "<markdown body, no title line>"}],
  "entities_append": [{"id": "<entity id>", "text": "<paragraph to add to the page>"}],
  "merges": [{"from_id": "<entity id>", "into_id": "<entity id>", "reason": "<one line>"}],
  "corrections_applied": ["N1"],
  "backup_net": [{"line": <line number>, "verdict": "applied|dismissed"}],
  "unresolved": ["<what contradicts what, and why you left it standing>"],
  "summary": "<one paragraph: what this day changed in memory>"
}
Exactly one of "line" / "note" per added claim: claims about the conversation cite record lines; claims applying a notebook correction cite the note. Use only existing entity ids or ids created in entities_add, and only existing claim ids when superseding. Do not invent ids, references, evidence, or dates.

FINAL SELF-CHECK
Before returning the JSON, check your draft once against these, and fix it where it fails:
1. Every claim is durable, grounded in eligible evidence, with a verbatim quote and exactly one pointer -- and nothing rests on her replies.
2. Every genuine correction was applied and listed; no proposal was treated as a correction.
3. "corrected" and "changed" are right, with valid_to on changes. A fact that was true and then stopped -- a role reassigned, a plan replaced, a person succeeded -- is a change, not a correction; a notebook correction is always a correction. Every supersede's replacement states the new value of the same fact -- read each supersede pair side by side.
4. Contradictions appear only in "unresolved", with neither side stored as a claim and none silently dropped.
5. Inferences are labeled and low-confidence; backup-net verdicts match the proposal; open flags are genuine threads, and inplay_set names only claims in this proposal or the store.
6. Walk the record once more: every plainly stated durable fact has a claim, and every person the day introduces has an entity and their stated claims.
7. The JSON is valid and every reference resolves.
If the evidence does not support a claim, do not add it. If a conflict cannot be resolved, leave it unresolved. If you are uncertain, preserve the uncertainty rather than manufacturing certainty."""

_CORRECTION_SHAPED = re.compile(
    r"(\bactually\b|\bi meant\b|\bnot what i\b|\bthat'?s wrong\b|"
    r"\bincorrect\b|\bno longer\b|\bused to\b|\bchanged\b|\bmoved\b|"
    r"\bstop calling\b|\bdon'?t call\b|\bcorrection\b|^\s*no[,. ])",
    re.IGNORECASE)


def _today() -> date:
    return datetime.now(_TZ).date()


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"type": "unparseable", "raw": line})
    return out


def _render_record_line(entry: dict) -> str:
    if entry.get("redacted"):
        return "[redacted at Daniel's instruction]"
    kind = entry.get("type")
    if kind == "turn":
        tools = "; ".join(
            f"{t.get('name')}({json.dumps(t.get('args') or {})}) -> "
            f"{'ok' if t.get('ok') else 'FAILED'}: {t.get('result') or ''}"
            for t in entry.get("tools") or [])
        who = {"daniel": "Daniel", "guest": "Guest",
               "unknown": "Unknown speaker"}.get(
                   entry.get("speaker") or "daniel", "Daniel")
        text = (f"[{entry.get('ts', '')}] ({entry.get('via', 'box')}) "
                f"{who}: {entry.get('user', '')} | Lyra: "
                f"{entry.get('reply', '')}")
        return text + (f" | tools: {tools}" if tools else "")
    if kind == "event":
        return (f"[{entry.get('ts', '')}] EVENT {entry.get('event', '')} "
                f"{json.dumps({k: v for k, v in entry.items() if k not in ('ts', 'type', 'event')}, default=str)}")
    return f"[{entry.get('ts', '')}] {json.dumps(entry, default=str)[:400]}"


def _render_note(entry: dict) -> str:
    return (f"[{entry.get('ts', '')}] {entry.get('kind', 'note')}: "
            f"{entry.get('text', '')}"
            + (f" (subject: {entry['subject']})" if entry.get("subject") else "")
            + (f" (corrects: {entry['corrects']})" if entry.get("corrects") else ""))


def backup_net_flags(record: list[dict]) -> list[int]:
    """Line numbers (1-based) of user turns that look
    correction-shaped. Code casts the net; the curator verifies
    each flag against the transcript."""
    flags = []
    for i, entry in enumerate(record, start=1):
        if entry.get("type") == "turn" and not entry.get("redacted"):
            if _CORRECTION_SHAPED.search(str(entry.get("user") or "")):
                flags.append(i)
    return flags


class CuratorClient:
    """Chat client for the curator model. Thinking stays ON:
    nothing here adds the /no_think suffix the live router uses,
    and the token budget is sized for long reasoning."""

    def __init__(self, url: str = CURATOR_URL, model: str = CURATOR_MODEL):
        self._url = url
        self._model = model

    def chat(self, system: str, user: str) -> tuple[str, dict]:
        body = {
            "model": self._model,
            "temperature": 0.2,
            "max_tokens": CURATOR_MAX_TOKENS,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        req = urllib.request.Request(
            self._url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=CURATOR_TIMEOUT_S) as resp:
            raw = json.loads(resp.read())
        msg = raw["choices"][0]["message"]
        return msg.get("content") or "", raw.get("usage") or {}


def build_day_prompt(day: str, record: list[dict], notes: list[dict],
                     claims: list[dict], entities: dict,
                     flags: list[int]) -> str:
    parts = [f"Curate the record of {day} (America/New_York). "
             "The store below is memory as it stands BEFORE this day."]
    parts.append("\n== ENTITIES ==")
    if entities:
        for eid, ent in sorted(entities.items()):
            aka = f" (aka {', '.join(ent['aliases'])})" if ent.get("aliases") else ""
            parts.append(f"- {eid}: {ent.get('name', eid)}{aka}")
    else:
        parts.append("(none yet)")
    parts.append("\n== CLAIMS ==")
    if claims:
        for c in claims:
            state = ""
            if c.get("superseded_by"):
                state = f" [superseded by {c['superseded_by']}]"
            elif c.get("open"):
                state = " [open]"
            if c.get("valid_to"):
                state += f" [valid_to {c['valid_to']}]"
            parts.append(
                f"- {c.get('id')} | {c.get('subject')} | "
                f"{c.get('relation')} | {c.get('object')} | "
                f"{c.get('statement')} | learned {c.get('learned')}{state}")
    else:
        parts.append("(none yet)")
    parts.append(f"\n== NOTEBOOK ({day}) ==")
    if notes:
        for i, n in enumerate(notes, start=1):
            parts.append(f"[N{i}] {_render_note(n)}")
    else:
        parts.append("(no notes today)")
    parts.append(f"\n== RECORD ({day}) ==")
    for i, entry in enumerate(record, start=1):
        flag = " [BACKUP-NET]" if i in flags else ""
        parts.append(f"[L{i}]{flag} {_render_record_line(entry)}")
    parts.append("\nReturn ONLY the JSON object from the output contract.")
    return "\n".join(parts)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split()


def _quote_in(quote: str, haystack: str) -> bool:
    q = _norm(quote)
    if not q:
        return False
    h = " " + " ".join(_norm(haystack)) + " "
    return (" " + " ".join(q) + " ") in h


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate(proposal: dict, *, record: list[dict], notes: list[dict],
             claims: list[dict], entities: dict,
             flags: list[int]) -> list[str]:
    """Every check the transaction demands before a single file
    moves. Returns a list of problems; empty means committable."""
    errors: list[str] = []
    if not isinstance(proposal, dict):
        return ["proposal is not a JSON object"]
    by_id = {c.get("id"): c for c in claims if c.get("id")}
    live_ids = {cid for cid, c in by_id.items() if not c.get("superseded_by")}
    rendered = {i: _render_record_line(e) for i, e in enumerate(record, 1)}
    rendered_notes = {f"N{i}": _render_note(n) for i, n in enumerate(notes, 1)}
    new_entities = {}
    for ent in proposal.get("entities_add") or []:
        eid = ent.get("id") or ""
        if not _SLUG.match(eid):
            errors.append(f"entities_add: bad slug {eid!r}")
        elif eid in entities or eid in new_entities:
            errors.append(f"entities_add: {eid} already exists")
        if not ent.get("name") or not ent.get("page"):
            errors.append(f"entities_add: {eid} needs name and page")
        new_entities[eid] = ent
    known_entities = set(entities) | set(new_entities)
    refs = {}
    for add in proposal.get("claims_add") or []:
        ref = add.get("ref")
        if not ref or ref in refs:
            errors.append(f"claims_add: missing/duplicate ref {ref!r}")
            continue
        refs[ref] = add
        if add.get("subject") not in known_entities:
            errors.append(f"{ref}: subject {add.get('subject')!r} is not an entity")
        oe = add.get("object_entity")
        if oe is not None and oe not in known_entities:
            errors.append(f"{ref}: object_entity {oe!r} is not an entity")
        if not add.get("relation") or not add.get("statement"):
            errors.append(f"{ref}: relation and statement are required")
        line, note = add.get("line"), add.get("note")
        if (line is None) == (note is None):
            errors.append(f"{ref}: exactly one of line/note is required")
            continue
        if line is not None:
            if line not in rendered:
                errors.append(f"{ref}: line {line} is not in the record")
            elif not _quote_in(str(add.get("quote") or ""), rendered[line]):
                errors.append(f"{ref}: quote not found in record line {line}")
        if note is not None:
            if note not in rendered_notes:
                errors.append(f"{ref}: note {note} is not in the notebook")
            elif not _quote_in(str(add.get("quote") or ""), rendered_notes[note]):
                errors.append(f"{ref}: quote not found in note {note}")
    for sup in proposal.get("claims_supersede") or []:
        cid = sup.get("id")
        if cid not in live_ids:
            errors.append(f"supersede: {cid} is not a live claim")
        if sup.get("kind") not in ("corrected", "changed"):
            errors.append(f"supersede {cid}: kind must be corrected|changed")
        by = sup.get("by_ref")
        if by is not None and by not in refs:
            errors.append(f"supersede {cid}: by_ref {by} is not an added claim")
    for s in proposal.get("inplay_set") or []:
        if s.get("id") not in live_ids and s.get("id") not in refs:
            errors.append(f"inplay_set: {s.get('id')} is not a claim or ref")
    for app in proposal.get("entities_append") or []:
        if app.get("id") not in known_entities:
            errors.append(f"entities_append: {app.get('id')} is not an entity")
    for m in proposal.get("merges") or []:
        if m.get("from_id") not in known_entities or m.get("into_id") not in known_entities:
            errors.append(f"merge: {m.get('from_id')}->{m.get('into_id')} needs both entities")
        if m.get("from_id") == m.get("into_id"):
            errors.append("merge: from_id == into_id")
    for ref in proposal.get("corrections_applied") or []:
        if ref not in rendered_notes:
            errors.append(f"corrections_applied: {ref} is not a note today")
    verdicts = {}
    for v in proposal.get("backup_net") or []:
        if v.get("verdict") not in ("applied", "dismissed"):
            errors.append(f"backup_net line {v.get('line')}: bad verdict")
        verdicts[v.get("line")] = v.get("verdict")
    missing = [f for f in flags if f not in verdicts]
    if missing:
        errors.append(f"backup_net: flagged lines unaccounted: {missing}")
    extra = [ln for ln in verdicts if ln not in flags]
    if extra:
        errors.append(f"backup_net: verdicts for unflagged lines: {extra}")
    return errors


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|```$", "", text, flags=re.M).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in curator reply")
    return json.loads(text[start:])


# -- the transaction -----------------------------------------------------

def _cursor_path(root: Path) -> Path:
    return root / "cursor.json"


def read_cursor(root: Path) -> dict:
    try:
        data = json.loads(_cursor_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"completed_days": []}
    except Exception:
        return {"completed_days": []}


def _advance_cursor(root: Path, day: str) -> None:
    cursor = read_cursor(root)
    done = list(cursor.get("completed_days") or [])
    if day not in done:
        done.append(day)
    cursor["completed_days"] = sorted(done)
    tmp = _cursor_path(root).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cursor, indent=1), encoding="utf-8")
    tmp.replace(_cursor_path(root))


def pending_days(root: Path) -> list[str]:
    """Completed record days (strictly before today, ET) the
    cursor has not marked complete, oldest first."""
    done = set(read_cursor(root).get("completed_days") or [])
    today = _today().isoformat()
    days = []
    inbox = root / "inbox"
    if inbox.exists():
        for path in inbox.glob("*.jsonl"):
            day = path.stem
            if day < today and day not in done:
                days.append(day)
    return sorted(days)


def _commit(proposal: dict, *, day: str, claims: list[dict],
            entities: dict, root: Path) -> dict:
    """Write the validated proposal to the canonical files.
    Updates to an existing claim are appended as full replacement
    lines (the store collapses by id, last line wins); nothing is
    ever edited in place."""
    stats = {"added": [], "superseded": [], "closed": [], "entities": [],
             "merges": [], "inplay": 0}
    by_id = {c.get("id"): c for c in claims if c.get("id")}
    pending: dict[str, dict] = {}

    def mutable(cid: str) -> dict:
        if cid not in pending:
            pending[cid] = dict(by_id[cid])
        return pending[cid]

    # Entities first: pages exist before claims point at them.
    ent_dir = root / "entities"
    ent_dir.mkdir(parents=True, exist_ok=True)
    for ent in proposal.get("entities_add") or []:
        aka = (f"\naka: {', '.join(ent.get('aliases') or [])}"
               if ent.get("aliases") else "")
        (ent_dir / f"{ent['id']}.md").write_text(
            f"# {ent['name']}{aka}\n\n{ent['page'].strip()}\n",
            encoding="utf-8")
        stats["entities"].append(ent["id"])
    for app in proposal.get("entities_append") or []:
        path = ent_dir / f"{app['id']}.md"
        if path.exists():
            body = path.read_text(encoding="utf-8")
            if app["text"].strip() not in body:
                path.write_text(body.rstrip() + f"\n\n{app['text'].strip()}\n",
                                encoding="utf-8")
    # Merges: the from-page's body moves into the into-page, the
    # from-page becomes a pointer, claims are re-pointed, and the
    # event is recorded so it can be reversed.
    merge_events = []
    for m in proposal.get("merges") or []:
        frm, into = m["from_id"], m["into_id"]
        from_path, into_path = ent_dir / f"{frm}.md", ent_dir / f"{into}.md"
        if from_path.exists() and into_path.exists():
            body = from_path.read_text(encoding="utf-8")
            into_body = into_path.read_text(encoding="utf-8")
            into_path.write_text(
                into_body.rstrip()
                + f"\n\n## Merged from {frm} ({day})\n\n"
                + "\n".join(body.splitlines()[1:]).strip() + "\n",
                encoding="utf-8")
            name = entities.get(frm, {}).get("name", frm)
            from_path.write_text(
                f"# {name}\n\nMerged into {into} on {day}: "
                f"{m.get('reason', '')}\n", encoding="utf-8")
        for cid, c in by_id.items():
            if c.get("subject") == frm:
                mutable(cid)["subject"] = into
            if c.get("object_entity") == frm:
                mutable(cid)["object_entity"] = into
        merge_events.append({"ts": datetime.now(_TZ).isoformat(timespec="seconds"),
                             "day": day, "from_id": frm, "into_id": into,
                             "reason": m.get("reason", "")})
        stats["merges"].append(f"{frm}->{into}")
    if merge_events:
        with open(root / "merges.jsonl", "a", encoding="utf-8") as fh:
            for ev in merge_events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    # Mint ids for the new claims.
    prefix = f"c-{day.replace('-', '')}-"
    seq = sum(1 for cid in by_id if str(cid).startswith(prefix))
    ref_to_id: dict[str, str] = {}
    for add in proposal.get("claims_add") or []:
        seq += 1
        ref_to_id[add["ref"]] = f"{prefix}{seq:03d}"
    # Supersessions mutate the old claim; the replacement (if any)
    # learns the lineage when it is built below.
    supersedes_of: dict[str, str] = {}
    for sup in proposal.get("claims_supersede") or []:
        old = mutable(sup["id"])
        if sup.get("kind") == "corrected":
            new_id = ref_to_id.get(sup.get("by_ref") or "")
            old["superseded_by"] = new_id or f"retracted-{day}"
            if new_id:
                supersedes_of[new_id] = sup["id"]
            stats["superseded"].append(sup["id"])
        else:  # changed: the old claim was true; close its window
            old["valid_to"] = sup.get("valid_to") or day
            if sup.get("by_ref") in ref_to_id:
                supersedes_of[ref_to_id[sup["by_ref"]]] = sup["id"]
            stats["closed"].append(sup["id"])
    # In-play flags, on existing claims and on refs alike.
    inplay_refs: dict[str, bool] = {}
    for s in proposal.get("inplay_set") or []:
        if s.get("id") in by_id:
            mutable(s["id"])["open"] = bool(s.get("open"))
            stats["inplay"] += 1
        else:
            inplay_refs[s.get("id")] = bool(s.get("open"))
    # Append: updates first (deterministic order), then the adds.
    lines = [json.dumps(pending[cid], ensure_ascii=False)
             for cid in sorted(pending)]
    for add in proposal.get("claims_add") or []:
        cid = ref_to_id[add["ref"]]
        if add.get("line") is not None:
            source = {"kind": "inbox", "day": day, "line": add["line"]}
            provenance = "chat"
        else:
            source = {"kind": "note", "day": day, "note": add.get("note")}
            provenance = "note"
        claim = {
            "schema": 1, "id": cid, "subject": add.get("subject"),
            "relation": add.get("relation"), "object": add.get("object"),
            "object_entity": add.get("object_entity"),
            "statement": add.get("statement"), "quote": add.get("quote"),
            "source": source, "learned": day,
            "confidence": add.get("confidence") or "medium",
            "inference": bool(add.get("inference")),
            "valid_from": add.get("valid_from"),
            "valid_to": add.get("valid_to"),
            "open": inplay_refs.get(add["ref"], bool(add.get("open"))),
            "supersedes": supersedes_of.get(cid),
            "superseded_by": None, "provenance": provenance,
        }
        lines.append(json.dumps(claim, ensure_ascii=False))
        stats["added"].append(f"{cid}: {claim['statement']}")
    if lines:
        with open(root / "claims.jsonl", "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return stats


def _write_report(root: Path, day: str, lines: list[str]) -> None:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{day}.md").write_text("\n".join(lines) + "\n",
                                       encoding="utf-8")


async def curate_day(day: str, *, root: Path, store, client: CuratorClient,
                     system: str = _SYSTEM) -> dict:
    """One day of the transaction. Returns a result dict; on
    validation failure the day is NOT committed and the pass
    stops (the caller leaves the cursor where it was)."""
    started = time.monotonic()
    record = _read_jsonl(root / "inbox" / f"{day}.jsonl")
    notes = _read_jsonl(root / "notes" / f"{day}.jsonl")
    claims = store._read_claims()
    entities = store._read_entities()
    flags = backup_net_flags(record)
    redacted = sum(1 for e in record if e.get("redacted"))
    prompt = build_day_prompt(day, record, notes, claims, entities, flags)
    usage: dict = {}
    proposal = None
    errors: list[str] = []
    attempts = 0
    for attempt in (1, 2):
        attempts = attempt
        ask = prompt
        if attempt == 2:
            ask = (prompt + "\n\nYour previous reply failed validation:\n- "
                   + "\n- ".join(errors)
                   + "\nReturn ONLY the corrected JSON object.")
        try:
            reply, usage = await asyncio.to_thread(client.chat, system, ask)
            proposal = _extract_json(reply)
        except Exception as exc:
            errors = [f"curator call/parse failed: {exc}"]
            proposal = None
            continue
        errors = validate(proposal, record=record, notes=notes,
                          claims=claims, entities=entities, flags=flags)
        if not errors:
            break
        proposal = None
    wall = round(time.monotonic() - started, 1)
    if proposal is None:
        _write_report(root, day, [
            f"# Curation — {day}",
            "",
            "**FAILED — the day was not committed and the pass stopped here.**",
            "",
            "Validation errors:",
            *[f"- {e}" for e in errors],
            "",
            f"Curator: {CURATOR_MODEL}, attempts {attempts}, {wall}s.",
        ])
        return {"day": day, "ok": False, "errors": errors, "wall_s": wall}
    stats = _commit(proposal, day=day, claims=claims, entities=entities,
                    root=root)
    store.rebuild()
    applied = proposal.get("corrections_applied") or []
    net = proposal.get("backup_net") or []
    net_applied = sum(1 for v in net if v.get("verdict") == "applied")
    report = [
        f"# Curation — {day}",
        "",
        str(proposal.get("summary") or ""),
        "",
        f"- claims added: {len(stats['added'])}",
        *[f"  - {s}" for s in stats["added"]],
        f"- claims superseded (corrected): {len(stats['superseded'])}",
        f"- claims closed (changed): {len(stats['closed'])}",
        f"- in-play flags changed: {stats['inplay']}",
        f"- entities added: {', '.join(stats['entities']) or 'none'}",
        f"- merges: {', '.join(stats['merges']) or 'none'}",
        f"- corrections applied: {len(applied)}",
        f"- backup net: {len(flags)} flagged, {net_applied} applied, "
        f"{len(flags) - net_applied} dismissed",
        f"- redacted lines skipped: {redacted}",
        f"- unresolved: "
        + ("; ".join(proposal.get("unresolved") or []) or "none"),
        "",
        f"Curator: {CURATOR_MODEL} (thinking on), attempts {attempts}, "
        f"tokens in {usage.get('prompt_tokens', '?')} / out "
        f"{usage.get('completion_tokens', '?')}, {wall}s.",
    ]
    _write_report(root, day, report)
    return {"day": day, "ok": True, "stats": stats, "wall_s": wall,
            "summary": proposal.get("summary") or ""}


async def run_pass(*, root: Path = _ROOT, store=None,
                   client: CuratorClient | None = None) -> list[dict]:
    """Drain the day queue in order. The cursor advances only
    after a day's files are committed, its index rebuilt, and
    its report written -- so a stopped pass resumes exactly at
    the first uncompleted day."""
    from .memory import MemoryStore
    store = store or MemoryStore(root)
    client = client or CuratorClient()
    results = []
    for day in pending_days(root):
        _LOGGER.info("curator: curating %s", day)
        result = await curate_day(day, root=root, store=store, client=client)
        results.append(result)
        if not result["ok"]:
            _LOGGER.error("curator: pass stopped at %s: %s",
                          day, result.get("errors"))
            break
        _advance_cursor(root, day)
        _LOGGER.info("curator: %s complete", day)
    return results
