# Lyra Memory Build — Game Plan

_Written 2026-11-15 (Daniel + Lyra, main chat). Canonical copy: `~/Documents/LyraOS/memory/PLAN.md` on the Fedora — this is the design the memory build (D5) follows. Supersedes the parked "forward-log promotion loop" brainstorm with a concrete shape._

**Amendments (2026-11-21, Daniel):** working memory is full-history now (the 12-turn limit was removed 2026-11-16); the store reaches her by retrieval as well as by the injected card, with embeddings and a vector index (and maybe a graph); the curator field is narrowed to thinking-enabled candidates. **Status: Phase 0 was built 2026-11-15 and removed 2026-11-17 at Daniel's order -- the inbox is currently OFF; nothing else is built.**

## Principles

1. **Authority lives in the runtime, not the weights.** Facts live in files the code owns. The live model reads views; it is never the source of truth for a fact.
2. **Three pens.** Code holds the pen for the *record* (append-only, no interpretation). The sleep model holds the pen for *curation* (what becomes durable). Daniel holds the pen for *truth* (corrections and verdicts override everything). The live 9B holds no pen — she can propose, never promote.
3. **Move up, not down.** The curator is a step up from the live model in reasoning. Curation quality is the whole game; a weak curator pollutes the store permanently.
4. **Context is spent where it pays.** Her live window stays lean (the budget principle is why she works). The sleep model gets the big window — curation is where context buys correctness, and it runs unhurried, overnight, as a batch.
5. **Inferences are labeled, never baked in.** Derived conclusions carry their reasoning chain and stay distinguishable from facts, or they are derived on the spot at answer time.

## Layers

- **L0 — Inbox (the record).** Code appends every turn to a per-day JSONL log: timestamp, speaker, transcript, tool calls and their results, alarm/device events, and her memory proposals (see below). Immutable once written. This is the raw floor everything else traces back to — no fact exists only as a summary.
- **L1 — Claims (durable facts).** The sleep model extracts discrete claims: subject entity, relation, object, the source quote with a pointer into the L0 log, date learned, confidence, salience, and optional validity window (some facts expire). A correction creates a new claim that *supersedes* the old one; the chain is preserved, never edited away.
- **L2 — Entities.** People, places, and things get pages (plain markdown, portable — same rule as the second brain). Facts hang off entities; this is where cross-fact connections live (the two-hop problem: Daniel → works at → Stuart Jones Electric → located in → Jacksonville). Entity resolution — noticing two mentions are the same thing — is an explicit, named job of the sleep model.
- **L3 — State card (her view).** A compact digest the sleep model maintains: current standing facts, open threads, near-term context. Fixed token budget (target ≤ ~800 tokens). The core injects it into her live prompt; her rolling 12-turn window stays for conversational flow. She wakes each morning to a fresh card. The card is deliberately the *small* always-on layer -- the full claim store is not injected; she reaches it by retrieval (see "Retrieval" below). In plain terms: it is the sticky note she reads before every turn — not the memory itself, just the distilled "here's where things stand" she works from, the same role Muse's injected MEMORY.md plays in the main chat. Example shape:

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

**Her proposal queue.** During the day she may flag a moment as worth remembering (a router tool, e.g. `memory_flag`). The flag lands in the L0 inbox marked as *her proposal*. The sleep model judges it like everything else. She gets a voice in her own memory without holding the pen.

## Retrieval: how the store reaches her

Daniel's direction (2026-11-16 discussion, folded in 2026-11-21): memory does not have to *live* in her context to be hers. Beyond the small injected state card, the claim store is reached the way she reaches the weather -- with a tool.

- **The lookup tool.** A `memory_lookup` tool in the router (sibling of `weather_report`): she asks with a question or a subject; it returns claims as tool facts -- statement, source quote, date learned, confidence -- and she composes the answer. Tool results are the one channel she already handles correctly; a retrieved fact arrives with its evidence attached, never as anonymous prompt text.
- **Embeddings + vector index.** Claims and entity pages are embedded when the sleep pass writes them (and the query is embedded at lookup time) by a small local embedding model served beside the stack -- the single-instance rule applies to it like every shared service. The vectors live with the store (SQLite alongside the claim index; portable, inspectable, no external service). Lookup is hybrid: vector similarity for meaning, keyword/entity match for exact names, merged before anything reaches her.
- **Maybe a graph.** Claims are already (subject, relation, object) triples, so the store *is* a graph in embryo: entities are nodes, claims are edges, and a two-hop question (Daniel -> works at -> Stuart Jones Electric -> located in -> Jacksonville) is a walk. Whether that walk needs a dedicated graph structure or is served well enough by the claim index plus entity pages is an open question for the Phase 1 build -- the triples keep both options open, which is the point of storing them that way.

## Working memory: the turn limit and compaction

Three mechanisms cover three timescales, and they must not bleed into each other:

- **Turns (seconds–minutes).** Full session history (amended 2026-11-21: the 12-turn limit was removed 2026-11-16; the whole session goes to the model, bounded by the 30-minute idle clear and a large token safety valve). On its own it is still amnesia with a hard edge -- the edge just sits at the end of a session instead of mid-conversation.
- **Compaction (within one conversation).** If a session grows toward the safety valve, the oldest turns fold into a running session summary instead of vanishing; the live context becomes summary + most recent turns, so a long conversation can run indefinitely. The live model writes the summary at fold time -- cheap and immediate, and allowed to be imperfect, because of the separation below. The summary dies at the 30-minute idle clear, exactly like the history does today.
- **State card (across days).** Nightly and curated — see L3. It cannot carry intra-day continuity; that is compaction's job.

**Compaction never enters the record (Daniel, 2026-11-15) — this is what makes it safe.** The summary exists only inside the live model's open context window, as a context-management device. It is never written to the inbox: the log contains turns and events only, appended by code from what actually happened, and the curator reads that log — so the curator never sees a summary at all, and there is nothing to label or police. (An earlier draft logged summaries as typed "artifacts" and forbade the curator from citing them; Daniel's version is stronger — a seal by construction beats a rule a curator must obey.) Durable facts are minted in exactly one place: the curator, working from the full raw log. A summary can therefore be wrong without consequence beyond the conversation it serves, and the leak — a live-model summary hardening into a durable "fact" — cannot happen, because the summary never crosses into anything the durable side reads.

## The sleep cycle

- **Window.** Nightly, after the house is quiet — trigger on extended idle or a fixed hour (~02:00), with a hard finish deadline of 05:30 so she is always awake before the 06:00 alarm. The alarm path never depends on the sleep job.
- **Face.** While state = asleep, the TV plays her sleep animation (SLEEP_MOTION, haru_g_m19 — reserved 2026-11-15). The display state machine gains a third state: awake / off / asleep.
- **GPU swap.** The live llama-server (9B, 8K context) stops. A sleep llama-server loads the curator model with a large context (target 64K). Whisper and Kokoro stay running (single-instance rule) but idle. After curation, the sleep server stops and the live server restarts.
- **The pass.** The curator reads the whole day's inbox *plus* the existing claims and entity pages in the same window — this is why the big context matters: superseding a fact requires seeing the old claim and the new correction together, and resolving an entity requires seeing both mentions. It writes claims, updates entities, rebuilds the state card, and logs a short curation report (what it promoted, merged, superseded, and why) that Daniel can review.
- **Failure safety.** If the pass fails or overruns, she wakes on time with yesterday's state card. The inbox is append-only, so an uncurated day is never lost — it is curated the next night.

## The curator model

Requirements: a clear step up from the 9B in reasoning; large context; must run on the night-time GPU budget (16 GB VRAM, with system-RAM offload acceptable — it is a batch job, speed is secondary).

- The field (narrowed by Daniel, 2026-11-21): two thinking-enabled candidates. (a) The same model she runs live -- Nemotron Nano 9B v2 -- with thinking ENABLED for the night shift: live runs it with thinking off for speed, the curator trades that speed for reasoning, which is this option's "move up" step. (b) A 12B-class model, thinking enabled. Both fit the 16 GB card outright, so the GPU swap may shrink to a context-size change rather than a model swap with offload. Decided by bake-off, not by spec sheet -- and because thinking traces are long, the bake-off also scores tokens-per-night and wall-clock against the 05:30 deadline.
- **Bake-off protocol.** Once the inbox has real days in it, run each candidate over the same days of actual conversation logs. Grade: extraction precision (no invented facts), recall of durable facts, entity merges done correctly, corrections superseded correctly, and a planted two-hop connection (Stuart Jones test) found. Winner curates.
- **The pipeline is model-agnostic** (an OpenAI-compatible endpoint plus a context size). On the future Strix Halo box (128 GB unified), the curator becomes a 70B-class model with 128K context with no redesign — the swap is a config change.
- Model sourcing rule stands: official publisher repos (Daniel's Unsloth waiver was for the current 9B; a curator pick gets its own decision).

## Context plan

- **Live (9B):** stays at 8K. Brief + state card + tool results + session history. The state card is what grows her effective memory without growing the window. A stretch to 12–16K is optional later if VRAM measurements allow; it is not required by this design.
- **Sleep (curator):** 64K target on this hardware, 128K on the Halo. Whole day + claim store + entity pages in one view. This is the "don't rush" budget: curation quality is context-bound, so the context goes to the curator.
- **This design doc** is itself part of the context plan: the reasoning is written down so no future session has to re-derive it under pressure.

## Phases

- **Phase 0 — Turn on the inbox.** Core-side JSONL logging of turns and events (L0 only). No model change, no behavior change, nothing injected into her prompt. Highest value per effort: every day from switch-on is curatable later, including days before the rest exists.
- **Phase 1 -- Store, retrieval + state card.** Claim and entity formats on disk (markdown + a SQLite index, portable formats only) with embeddings and a vector index over claims and entity pages; the `memory_lookup` retrieval tool in the router; and the core change that injects the state card into the router prompt. An empty card and an empty store reproduce today's behavior exactly, so this lands safely before any curator exists.
- **Phase 2 — The sleep job.** Display state machine gains "asleep" (sleep animation wired), the GPU swap scripts, the curation pass, the curator bake-off on accumulated real logs, and the morning curation report.
- **Phase 3 — Her voice in it + Daniel's surface.** The `memory_flag` proposal tool for the live model; inference labeling in her answers (she can show the hops behind a derived belief); a dashboard memory tile where Daniel can read the state card and strike a claim — his truth pen, made literal.

Explicitly **not** in this plan: the live model curating itself; fine-tuning/LoRA on the logs (separate parked idea); any cloud curation (local-first stands).

## Open decisions (Daniel's, when the build starts)

- Sleep window: fixed hour vs. idle-triggered.
- Embedding model pick (small, local, served beside the stack).
- Graph: a dedicated graph structure for the claim triples, or walks over the claim index + entity pages (decide at the Phase 1 build).
- How much of the claim-store machinery to copy from Muse's stack vs. keep simpler and files-first.
