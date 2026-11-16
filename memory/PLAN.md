# Lyra Memory Build — Game Plan

_Written 2026-11-15 (Daniel + Lyra, main chat). Canonical copy: `~/Documents/LyraOS/memory/PLAN.md` on the Fedora — this is the design the memory build (D5) follows. Supersedes the parked "forward-log promotion loop" brainstorm with a concrete shape._

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
- **L3 — State card (her view).** A compact digest the sleep model maintains: current standing facts, open threads, near-term context. Fixed token budget (target ≤ ~800 tokens). The core injects it into her live prompt; her rolling 12-turn window stays for conversational flow. She wakes each morning to a fresh card.
- **L4 — Runtime state.** Commitments (alarm, notes, reminders) stay in code-owned state files, exactly as the alarm does today. Prose memory never becomes load-bearing for an action.

**Her proposal queue.** During the day she may flag a moment as worth remembering (a router tool, e.g. `memory_flag`). The flag lands in the L0 inbox marked as *her proposal*. The sleep model judges it like everything else. She gets a voice in her own memory without holding the pen.

## The sleep cycle

- **Window.** Nightly, after the house is quiet — trigger on extended idle or a fixed hour (~02:00), with a hard finish deadline of 05:30 so she is always awake before the 06:00 alarm. The alarm path never depends on the sleep job.
- **Face.** While state = asleep, the TV plays her sleep animation (SLEEP_MOTION, haru_g_m19 — reserved 2026-11-15). The display state machine gains a third state: awake / off / asleep.
- **GPU swap.** The live llama-server (9B, 8K context) stops. A sleep llama-server loads the curator model with a large context (target 64K). Whisper and Kokoro stay running (single-instance rule) but idle. After curation, the sleep server stops and the live server restarts.
- **The pass.** The curator reads the whole day's inbox *plus* the existing claims and entity pages in the same window — this is why the big context matters: superseding a fact requires seeing the old claim and the new correction together, and resolving an entity requires seeing both mentions. It writes claims, updates entities, rebuilds the state card, and logs a short curation report (what it promoted, merged, superseded, and why) that Daniel can review.
- **Failure safety.** If the pass fails or overruns, she wakes on time with yesterday's state card. The inbox is append-only, so an uncurated day is never lost — it is curated the next night.

## The curator model

Requirements: a clear step up from the 9B in reasoning; large context; must run on the night-time GPU budget (16 GB VRAM, with system-RAM offload acceptable — it is a batch job, speed is secondary).

- On the current box, candidates are in the 14B class fully in VRAM (Q4/Q8), or a 30–32B class at Q4 with partial offload (~19 GB — spills to RAM, slow but overnight-viable). Exact model decided by bake-off, not by spec sheet.
- **Bake-off protocol.** Once the inbox has real days in it, run each candidate over the same days of actual conversation logs. Grade: extraction precision (no invented facts), recall of durable facts, entity merges done correctly, corrections superseded correctly, and a planted two-hop connection (Stuart Jones test) found. Winner curates.
- **The pipeline is model-agnostic** (an OpenAI-compatible endpoint plus a context size). On the future Strix Halo box (128 GB unified), the curator becomes a 70B-class model with 128K context with no redesign — the swap is a config change.
- Model sourcing rule stands: official publisher repos (Daniel's Unsloth waiver was for the current 9B; a curator pick gets its own decision).

## Context plan

- **Live (9B):** stays at 8K. Brief + state card + tool results + 12 turns. The state card is what grows her effective memory without growing the window. A stretch to 12–16K is optional later if VRAM measurements allow; it is not required by this design.
- **Sleep (curator):** 64K target on this hardware, 128K on the Halo. Whole day + claim store + entity pages in one view. This is the "don't rush" budget: curation quality is context-bound, so the context goes to the curator.
- **This design doc** is itself part of the context plan: the reasoning is written down so no future session has to re-derive it under pressure.

## Phases

- **Phase 0 — Turn on the inbox.** Core-side JSONL logging of turns and events (L0 only). No model change, no behavior change, nothing injected into her prompt. Highest value per effort: every day from switch-on is curatable later, including days before the rest exists.
- **Phase 1 — Store + state card.** Claim and entity formats on disk (markdown + a SQLite index, portable formats only), and the core change that injects the state card into the router prompt. An empty card reproduces today's behavior exactly, so this lands safely before any curator exists.
- **Phase 2 — The sleep job.** Display state machine gains "asleep" (sleep animation wired), the GPU swap scripts, the curation pass, the curator bake-off on accumulated real logs, and the morning curation report.
- **Phase 3 — Her voice in it + Daniel's surface.** The `memory_flag` proposal tool for the live model; inference labeling in her answers (she can show the hops behind a derived belief); a dashboard memory tile where Daniel can read the state card and strike a claim — his truth pen, made literal.

Explicitly **not** in this plan: the live model curating itself; fine-tuning/LoRA on the logs (separate parked idea); any cloud curation (local-first stands).

## Open decisions (Daniel's, when the build starts)

- Sleep window: fixed hour vs. idle-triggered.
- First curator on the current Fedora as-is, or wait for the Halo.
- How much of the claim-store machinery to copy from Muse's stack vs. keep simpler and files-first.
