# Lyra Memory — The Plan

_Consolidated 2026-11-21 (Daniel + Lyra). This is the single document for Lyra's memory: every decision made through 2026-11-21 is folded into the body, not layered on as amendments. Canonical copy: `~/Documents/LyraOS/memory/PLAN.md` on the Fedora._

**Status: PLAN ONLY — nothing is built, and nothing gets built until Daniel says so.** Phase 0 (the inbox) was built 2026-11-15 and removed 2026-11-17 at Daniel's order; the inbox is currently OFF. Every other phase is unbuilt.

## Principles

1. **Authority lives in the runtime, not the weights.** Facts live in files the code owns. The live model reads views; it is never the source of truth for a fact.
2. **Two pens, and Daniel's voice.** Code holds the pen for the *record* (append-only, no interpretation). The curator holds the ONLY pen for *memory* — what becomes durable and what gets fixed is hers alone. Daniel holds no pen, and there is deliberately no memory-editing surface: his corrections happen in conversation and are filed automatically as a note to the curator the moment they land. The notebook is what supersedes: a correction note outranks the stored claim from the moment it is written — in retrieval that evening and in the curator's pass at night — because context gets summarized, and a correction must never depend on surviving a summary. The live model holds no pen either — she takes notes and proposes, never promotes.
3. **Move up, not down.** The curator is a step up from the live model in reasoning. Curation quality is the whole game; a weak curator pollutes the store permanently.
4. **Context is spent where it pays.** Her live window stays lean (the budget principle is why she works). The sleep model gets the big window — curation is where context buys correctness, and it runs unhurried, overnight, as a batch.
5. **Inferences are labeled, never baked in.** Derived conclusions carry their reasoning chain and stay distinguishable from facts, or they are derived on the spot at answer time.

## The record — L0

Everything durable traces back to an append-only record that code writes and nothing interprets:

- **Inbox.** Code appends every turn to a per-day JSONL log (`memory/inbox/YYYY-MM-DD.jsonl`): timestamp, speaker, transcript, tool calls and their results, alarm/device events, and her memory proposals. Immutable once written. No fact exists only as a summary. A "day" is the calendar day (America/New_York), midnight to midnight: at 00:00 the day's inbox and notebook close, the new day's open, and the completed day goes to the sleep pass.
- **Notebook.** The running brain's notes live in a parallel per-day stream (`memory/notes/YYYY-MM-DD.jsonl`) — also code-written, also append-only — so every day has two files: what was said, and what the running brain marked about it. Two kinds of notes: *proposals* (moments she judges worth remembering) and *corrections* (moments Daniel fixes a fact in conversation: what memory said, what he said instead, the quote — filed automatically the moment the correction happens, never an extra step for him). A correction note is an instruction to the curator: the supersession it calls for gets made.
- **Raw sources.** Material Daniel hands her through a dashboard upload spot (a mailbox, not an editor): chat exports, documents, anything text-shaped. An upload lands in `memory/sources/` as an immutable original with a manifest line (name, date uploaded, size, hash), and is chunked and embedded on arrival, so a source far bigger than any context window stays workable. A source is a document, not Daniel asserting facts in the room today: in-chat statements and notebook corrections outrank source-derived claims wherever they disagree.

## The store — L1 claims, L2 entities

- **L1 — Claims (durable facts).** The curator extracts discrete claims: subject entity, relation, object, the source quote with a pointer into the record (an inbox line, a notebook entry, or a source passage), date learned, confidence, salience, and an optional validity window (some facts expire). A correction creates a new claim that *supersedes* the old one; the chain is preserved, never edited away.
- **L2 — Entities.** People, places, and things get pages (plain markdown, portable). Facts hang off entities; this is where cross-fact connections live (the two-hop problem: Daniel → works at → Stuart Jones Electric → located in → Jacksonville). Entity resolution — noticing two mentions are the same thing — is an explicit, named job of the curator.
- **Embeddings + vector index.** Claims, entity pages, and source chunks are embedded when written (and a query is embedded at lookup time) by a small local embedding model served beside the stack — the single-instance rule applies to it like every shared service. The vectors live with the store (SQLite alongside the claim index; portable, inspectable, no external service). Retrieval is hybrid: vector similarity for meaning, keyword/entity match for exact names, merged before anything reaches her.
- **Maybe a graph.** Claims are already (subject, relation, object) triples, so the store is a graph in embryo: entities are nodes, claims are edges, and a two-hop question is a walk. Whether that walk needs a dedicated graph structure or is served well enough by the claim index plus entity pages is an open decision (below) — the triples keep both options open, which is the point of storing them that way.

## Her view — L3 state card, L4 runtime state

- **L3 — State card.** A compact digest the curator maintains: current standing facts, open threads, near-term context. Fixed token budget (target ≤ ~800 tokens). The core injects it into her live prompt; the full claim store is NOT injected — she reaches it by retrieval (below). She wakes each morning to a fresh card. In plain terms: it is the sticky note she reads before every turn — not the memory itself, just the distilled "here's where things stand" she works from. Example shape:

  ```
  STATE CARD — updated 2026-11-16 04:10 by curator
  Daniel: home Jacksonville (Pickettville Rd); work Corporate Square; alarm 06:00
  weekdays, leaves ~06:00, at work ~06:45. Cat: Gisele.
  Standing: probation to 2028-01-04, monthly check-ins. PO Alex — DOC
  recommendation request sent 2026-11-13, reply pending.
  Open threads: Kate Mesic consult (retry Mon); Jellyfin key needs recreating.
  Yesterday: unused models removed; sleep animation reserved for this cycle.
  ```
- **L4 — Runtime state.** Commitments (alarm, notes, reminders) stay in code-owned state files, exactly as the alarm does today. Prose memory never becomes load-bearing for an action.

## Working memory and the compaction seal

Three mechanisms cover three timescales, and they must not bleed into each other:

- **Turns (seconds–minutes).** Full session history — the whole session goes to the model, bounded by the 30-minute idle clear and a large token safety valve. On its own it is still amnesia with a hard edge; the edge just sits at the end of a session instead of mid-conversation.
- **Compaction (within one conversation).** If a session grows toward the safety valve, the oldest turns fold into a running session summary instead of vanishing; the live context becomes summary + most recent turns, so a long conversation can run indefinitely. The live model writes the summary at fold time — cheap and immediate, and allowed to be imperfect, because of the seal below. The summary dies at the 30-minute idle clear, exactly like the history does today.
- **State card (across days).** Nightly and curated — see L3. It cannot carry intra-day continuity; that is compaction's job.

**Compaction never enters the record (Daniel, 2026-11-15) — this is what makes it safe.** The summary exists only inside the live model's open context window, as a context-management device. It is never written to the inbox: the log contains turns and events only, appended by code from what actually happened, and the curator reads that log — so the curator never sees a summary at all, and there is nothing to label or police. Durable facts are minted in exactly one place: the curator, working from the full raw log. A summary can therefore be wrong without consequence beyond the conversation it serves, and the leak — a live-model summary hardening into a durable "fact" — cannot happen, because the summary never crosses into anything the durable side reads. Corrections are the sharp case, and they are safe by the same construction: a correction never lives only in context — code writes it to the notebook the moment it happens, on the record side of the seal — so no summary can carry it, mangle it, or drop it; the notebook, not the context, is what supersedes.

## Retrieval — how the store reaches her

Memory does not have to *live* in her context to be hers. Three channels:

1. **The state card** — always injected (above).
2. **Ambient injection (pushed, permissive).** She should not have to decide to remember. On each user turn the router embeds the incoming message (with a little of the thread around it) and queries the same index the lookup tool uses; the few claims that clear a relevance bar are placed quietly in her context as things that come to mind — statement and date, nothing more. She may use one, or none: nothing is owed a mention, nothing is announced as a memory, and below the bar nothing is injected at all. This is the channel that lets her say "yeah, she did that last week too" when Daniel mentions the cat — last week's claim surfaced because the topic called it up, not because she thought to run a lookup. Guards: a similarity threshold (silence is the default), a small cap, notebook corrections checked first (a corrected claim never surfaces in its stale form), dedupe against the state card and against claims already surfaced this session, and each claim's date rides along so she places things in time honestly instead of inventing the connective tissue.
3. **The lookup tool (pulled).** A `memory_lookup` tool in the router (sibling of `weather_report`): she asks with a question or a subject; it returns claims as tool facts — statement, source quote, date learned, confidence — and she composes the answer. Retrieval checks the notebook first: an unprocessed correction note touching the subject outranks the stored claim, and the tool returns the corrected fact (marked as Daniel's correction, pending curation) rather than the stale claim — the curator formalizes the supersession at night; she does not decide whether it counts. Tool results are the one channel she already handles correctly; a retrieved fact arrives with its evidence attached, never as anonymous prompt text.

## The sleep cycle

- **Window.** Fixed at midnight (Daniel, 2026-11-21): at 00:00 the completed day's logs close, she enters sleep, and the pass takes that day first; when she wakes, the new day's log is already open and collecting. NO finish deadline: the pass runs until the catch-up queue is drained, however long that takes. If she is still asleep come morning, she simply is not usable until it finishes — accepted, for now. The alarm is no part of this trade: it sits outside her, in code-owned clockwork (L4), and fires on schedule whether she is awake, asleep, or mid-curation.
- **Face.** While state = asleep, the TV plays her sleep animation (SLEEP_MOTION, haru_g_m19 — reserved 2026-11-15). The display state machine gains a third state: awake / off / asleep.
- **GPU swap.** The live llama-server (9B, 8K context) stops. A sleep llama-server loads the curator model with a large context (target 64K). Whisper and Kokoro stay running (single-instance rule) but idle. After curation, the sleep server stops and the live server restarts.
- **The pass — a day-by-day catch-up.** The curator works a queue, not just last night: starting from the first day not yet completed, she takes that day's inbox log *plus* that day's notebook, and reads them together with the existing claims and entity pages in the same window — this is why the big context matters: superseding a fact requires seeing the old claim and the correction together, and resolving an entity requires seeing both mentions. She applies everything that day calls for — promotions, merges, and every supersession the correction notes instruct — rebuilds the state card, marks the day complete, and moves to the next day, until every day through today is completed. Only then does she turn to raw sources: pending uploads in upload order, each read and mined for durable claims and marked curated when done — sources never jump ahead of days. A completion cursor (the last completed day) is the pass's only bookkeeping. Each completed day also gets its line in the curation report (what it promoted, merged, superseded, and why) that Daniel can review.
- **Failure safety.** If the pass fails (a crash or an error — there is no deadline to hit), she wakes with the latest state card; completed days stay completed, and the next run resumes at the first uncompleted day. An overrun is not a failure: the pass simply keeps going into the morning and she is unusable until the queue drains (see Window). The record is append-only, so no day is ever lost — a missed night just means a longer queue the next night.

## The curator

Requirements: a clear step up from the 9B in reasoning; large context; must run on the night-time GPU budget (16 GB VRAM, with system-RAM offload acceptable — it is a batch job, speed is secondary).

- **The field (narrowed by Daniel, 2026-11-21): two thinking-enabled candidates.** (a) The same model she runs live — Nemotron Nano 9B v2 — with thinking ENABLED for the night shift: live runs it with thinking off for speed; the curator trades that speed for reasoning, which is this option's "move up" step. (b) A 12B-class model, thinking enabled [the specific 12B is OPEN — decision 1 below]. Both fit the 16 GB card outright, so the GPU swap may shrink to a context-size change rather than a model swap with offload. Decided by bake-off, not by spec sheet — and because thinking traces are long, the bake-off also scores tokens-per-night and wall-clock: there is no hard deadline, but a curator that routinely runs into the morning leaves her unusable, so speed still counts.
- **Bake-off protocol.** Once the inbox has real days in it, run each candidate over the same days of actual conversation logs. Grade: extraction precision (no invented facts), recall of durable facts, entity merges done correctly, corrections superseded correctly, and a planted two-hop connection (Stuart Jones test) found. Winner curates.
- **The pipeline is model-agnostic** (an OpenAI-compatible endpoint plus a context size). On the future Strix Halo box (128 GB unified), the curator becomes a 70B-class model with 128K context with no redesign — the swap is a config change.
- Model sourcing rule stands: official publisher repos (Daniel's Unsloth waiver was for the current 9B; a curator pick gets its own decision).

## Context plan

- **Live (9B):** stays at 8K. Brief + state card + tool results + session history. The state card is what grows her effective memory without growing the window. A stretch to 16K is optional later if VRAM measurements allow; it is not required by this design.
- **Sleep (curator):** 64K target on this hardware, 128K on the Halo. Whole day + claim store + entity pages in one view. This is the "don't rush" budget: curation quality is context-bound, so the context goes to the curator.
- This design doc is itself part of the context plan: the reasoning is written down so no future session has to re-derive it under pressure.

## Phases

- **Phase 0 — Turn the inbox back on.** Core-side JSONL logging of turns and events (L0), plus the per-day notebook file. No model change, no behavior change, nothing injected into her prompt. Highest value per effort: every day from switch-on is curatable later, including days before the rest exists. (A first version was built 2026-11-15 and removed 2026-11-17; this phase rebuilds it as designed here.)
- **Phase 1 — Store, retrieval + state card.** Claim and entity formats on disk (markdown + a SQLite index, portable formats only) with embeddings and a vector index over claims and entity pages; the `memory_lookup` retrieval tool in the router; ambient injection of topic-relevant claims into her context on each user turn; and the core change that injects the state card into the router prompt. An empty card and an empty store reproduce today's behavior exactly, so this lands safely before any curator exists.
- **Phase 2 — The sleep job.** Display state machine gains "asleep" (sleep animation wired), the GPU swap scripts, the curation pass, the curator bake-off on accumulated real logs, and the morning curation report.
- **Phase 3 — Her voice in it.** The `memory_note` tool for the running brain (proposals + automatic correction notes); inference labeling in her answers (she can show the hops behind a derived belief). Deliberately NOT in this phase, or any phase: a memory-editing surface for Daniel — no dashboard tile, no strike-a-claim. His control surface is the conversation itself; the curation report is his window, not an editor.
- **Phase 4 — Raw sources.** The dashboard upload spot (a mailbox, not an editor); the `memory/sources/` store with its manifest; chunking + embedding on arrival; and the curator's source pass after the day queue, minting claims and entity material with source provenance.

Explicitly **not** in this plan: the live model curating itself; fine-tuning/LoRA on the logs (separate parked idea); any cloud curation (local-first stands).

## Open decisions

_The sweep list. Each gets decided, folded into the body above, and struck from here. When this list is empty, the plan is ready to build — and building still waits for Daniel's word._

1. **The 12B curator candidate:** which specific model joins the bake-off against the thinking-enabled 9B?
2. **Embedding model pick** (small, local, served beside the stack).
3. **Graph:** a dedicated graph structure for the claim triples, or walks over the claim index + entity pages?
4. **Claim-store machinery:** how much to borrow from Muse's stack vs keep simpler and files-first.
5. **State card budget:** is ~800 tokens the right cap?
6. **Ambient injection tuning:** the relevance bar and the per-turn cap — set values now, or calibrate at build time against real claims?
7. **Curation report:** where Daniel reads it — a file, the dashboard, a spoken morning summary?
8. **Memory on the Muse brain:** when the dashboard brain switch is set to Muse, do the state card, ambient injection, and lookup apply to those turns too — and how, given that path takes no tool calls?
9. **Correction-detection backup:** the running brain's judgment files the correction notes — should code also flag candidate corrections from the day's transcript for the curator to check, as a safety net?
10. **Upload limits:** file types and size caps for the source spot.
