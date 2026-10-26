# Jev triage — parked

**Status (2026-10-26): parked. Production stays on the Haiku agents.**

The Jev integration is in the repo and off: `JEV_MODE=off`, no
`TYPESAFE_API_KEY`. With Jev off, every flow behaves as before (see "What
runs with Jev off"). Two replays against production history showed Jev is
only good enough for one decision — dropping obvious non-tasks — and that
decision saves too little to justify the vendor, the model and the nightly
refit right now. This document keeps what was built, what was measured, and
what to do if the idea comes back.

Jev (TypeSafe AI, `POST https://api.typesafe.ai/v1/systemone`) is a hosted
classifier:

- **Input:** a `state` (text or JSON) plus named questions — `choice` (up
  to 255 options), `score` (up to 10 ordered levels) or `noul` (yes/no).
- **Output:** for every question, a probability for each option.
- **Limits:** it writes no text and compares dates as text.
- **Price:** $0.042 per million input tokens; output is free.
- **Accuracy claim:** TypeSafe's benchmarks put it near Sonnet level.

## Why it's parked

The goal was to let Jev make every closed decision (task or not, duplicate,
label, estimate, due date) and keep the LLM for writing text. Measured on
production history, only the not-a-task drop holds up:

| Decision | Best result (out-of-fold, inputs that reached the agent) | Target | Verdict |
|---|---|---|---|
| Drop a non-task | 98.7 % precision at 46 % coverage (≥ 0.97); 100 % at 18 % (≥ 0.99) | 98 % | Works |
| Create a task | ~30 % precision at a few % coverage | 90 % | Far off |
| Match an existing task | 62–72 % on Gmail, 10–40 % on Slack | 95 % | Far off |
| Label | 77 % overall; 87 % at p ≥ 0.6 (80 % coverage); 93 % at p ≥ 0.8 (60 %) | — | Worse than the agent (4 label corrections in the UI, ever) |
| Estimate | off by 2× against the user's 10 corrections; the agent by 1.5× | — | Worse than the agent |

What the drop would buy:

- **Volume:** about 6 inputs a day reach the agent (1,137 Gmail/Slack in
  180 days). Dropping 46 % of them saves about 3 Haiku runs a day on
  background mail — a small cost saving, and no latency gain anyone sees.
- **Cost:** about one lost task per half-year. At ≥ 0.97 the model wrongly
  drops 7 of 1,137 inputs:
  - 3 Slack @channel messages the agent missed too (the user promoted them);
  - 1 @channel message the agent did turn into a task;
  - 3 follow-ups that closed tasks: a "return complete" email and two GitHub
    issue updates. The live policy checks for a matching task before
    dropping, so these may not have been dropped; the replay doesn't
    simulate that step.
- **Manual creation** wouldn't get faster without worse labels and
  estimates.

## What was measured

### Production baseline (2026-10-25)

- **Last 60 days:** 691 inputs reached the new-input agent, the cosine
  auto-decide dropped 358, 139 were manual extractions and 30 were thread
  follow-ups.
- **All time:** 4,128 inputs. Gmail and Slack are 86 % `not_task` (3,275 of
  3,793). There are 87 manual overrides and 7 `is_task` corrections in
  `task_overrides`.
- **Manual creation:** P50 4.2 s from POST to the processed row (last 120
  days, rows over 10 minutes excluded).

### Replay v1 (2026-10-25, 533 inputs, one `is_task` question, "task" first)

| | Gmail | Slack |
|---|---|---|
| Always "not a task" | ~87 % | ~95 % |
| Jev | 77.6 % | 65.7 % |
| Agent (upper bound) | 93.8 % | 95.0 % |

Jev called many non-tasks tasks (create precision 9–24 %). The causes were
the first-option lean plus wording that made any question a task.

### Replay v2 (2026-10-26, 1,842 Gmail/Slack inputs over 180 days)

Changes for v2:

- the task question is asked in both orders and averaged;
- six narrow yes/no signal questions;
- precedents and sender history moved out of Jev's state into a logistic
  regression ("triage model") fitted on the user's history;
- candidate tasks carry their labels.

Accuracy at p = 0.5 (linked to a task, or not):

| | Gmail (n=1,374) | Slack (n=468) |
|---|---|---|
| New tasks / follow-ups / not a task | 82 / 76 / 1,216 | 27 / 3 / 438 |
| Always "not a task" | 88.5 % | 93.6 % |
| Jev choice (both orders) | 77.2 % | 82.9 % |
| Triage model (out-of-fold) | 90.2 % | 94.4 % |
| Agent (upper bound) | 93.2 % | 94.7 % |

The model's weights, largest first:

| Feature | Weight |
|---|---|
| `not_task_precedent` | −5.90 |
| `task_precedent` | +3.46 |
| bias | +2.17 |
| `slack` | −1.10 |
| `sender_task_share` | +0.68 |
| `others_act` | +0.46 |
| `fyi_only` | +0.40 |
| `asks_for_action` | +0.33 |
| `is_task` | +0.29 |
| `directed_at_me` | +0.20 |
| `automated` | −0.19 |
| `sender_seen` | +0.07 |
| `acknowledgement` | +0.02 |
| `addressed_to_user` | 0.00 |

The model runs mostly on the user's history (precedent similarity). Jev's
answers add little, so a model without Jev might do nearly as well — this
is untested.

**Only the gray zone counts.** 647 of the 1,842 inputs (35 %) never reached
the agent: the cosine gate dropped 574, GitHub suppression 61 and
auto-duplicate 12. On those, any drop looks nearly perfect, and they
inflate the report. On the 1,137 inputs Jev would actually see in
production:

| Drop threshold | Model precision / coverage | Wrongly dropped | Jev choice alone |
|---|---|---|---|
| ≥ 0.90 | 96.9 % / 75 % | 16 new tasks, 10 follow-ups | 94.6 % / 61 % |
| ≥ 0.95 | 97.9 % / 58 % | 9 new tasks, 5 follow-ups | 95.3 % / 50 % |
| ≥ 0.97 | 98.7 % / 46 % | 4 new tasks, 3 follow-ups | 95.7 % / 43 % |
| ≥ 0.99 | 100 % / 18 % | none | 97.3 % / 30 % |

By source at ≥ 0.97:

- **Gmail:** 98.8 % at 37 % coverage, losing 0 new tasks and 3 follow-ups.
- **Slack:** 98.5 % at 61 % coverage, losing 4 new tasks.

**Labels:** 371 tasks had a label, but 119 of those labels aren't in
today's catalog, or have no description (`Reise`), so Jev couldn't have
picked them. On the gradable ones: 77 % correct overall, 87 % at p ≥ 0.6
and 93 % at p ≥ 0.8.

**Estimates:** median |log2(estimate / truth)|.

| Against | Jev | Agent |
|---|---|---|
| The user's corrections (n=10) | 1.00 | 0.58 |
| Observed durations (n=1) | 1.52 | 1.52 |

Observed durations need "still working → done" check-ins, and only 5
exist.

### Lessons for any future evaluation

- **The ground truth is agent-shaped.** It's the agent's decision unless the
  user corrected it, so the agent's score is an upper bound and some of
  Jev's "errors" are uncorrected agent errors. Read the disagreements
  (`--csv`).
- **Evaluate only what the system would see.** Exclude inputs the free
  gates decide (`branch` = `auto_not_task`, `github_suppressed`,
  `auto_duplicate`), and simulate the full policy, matching before dropping.
- **Separate new tasks from follow-ups.** A dropped follow-up loses a link
  or an auto-close; a dropped new task loses a task.
- **Compare against the trivial baseline.** At 86–94 % not-a-task,
  "always no" beats a naive classifier.
- **Check the label catalog.** Labels drift: retired names and labels
  without descriptions can't be predicted.
- **Estimates have almost no ground truth yet.** Only the user's
  corrections (about 10) and a handful of check-ins.
- **Costs:** a 2,000-input replay with about 34 questions per input is a
  few million tokens, well under a dollar.

### Jev behaviour worth remembering

- **First-option lean.** Jev favours the first option in a choice. Ask
  important choices in both orders and average them, or put the cheaper
  mistake first.
- **Literal reading.** It answers the words, not the intent. "Reply with an
  answer" made every channel question a task.
- **Counts, numbers and indirection are weak spots** ("sender: 14 not-task",
  "a similar input wasn't a task"). Keep them out of the state and give
  them to a downstream model.
- **Dates work as parts.** Month, day, weekday and hour as choices, with
  the calendar math in code (TypeSafe's date-extraction pattern). This
  worked well in design; it was never measured against history.
- **Use the option's probability, not `confidence`.** `confidence` depends
  on the number of options; a probability means the same everywhere.
- **Pin the model** (`jev-1.14.0`); `jev-latest` moves on release.

## If you pick it up again

**Worth revisiting when:**

- the agent's volume or cost grows a lot;
- a newer Jev version reads intent better (re-run the replay with the new
  pinned model);
- the hand-off plan needs a cheap classifier for "can an agent do this?".
  That question is separate from triage and wasn't measured.

**Next experiment** (one replay run):

1. **Evaluate, fit and publish only on gray-zone inputs** (original
   `branch` = `new_input`). **Do not run `--publish` as it is today**: it
   fits thresholds including the free-gate inputs, which makes the drop
   threshold too lenient.
2. **Simulate the full policy** (`policy.decide_new_input`), so the
   matching check runs before a drop.
3. **Ablation:** also fit a history-only model (precedent similarity,
   sender share, `directed_at_me`, source) without Jev.
   - **History-only gets close:** skip Jev and ship a free drop gate in
     front of the agent.
   - **Jev clearly adds to it:** enable drop-only. Keep creation, matching,
     manual creation and fields with the agent: set `create` disabled, and
     don't route manual entries to `triage.manual`.

**Then:** `--publish` → `JEV_MODE=shadow` for a week → `JEV_MODE=on`.
`JEV_CLOSE_ENABLED` stays off.

**Independent of Jev, still worth doing:**

- **Faster manual creation without quality loss:** create the task at once
  from the typed text, and let the agent fill label, estimate and due date
  a few seconds later.

## What's in the code

| Path | Role |
|---|---|
| `agent/helpers/jev.py` | Client (raw httpx): `Choice` / `Score` / `Noul`, `ask`, `JevUnavailable`; one retry on 429/529/5xx; Langfuse span per call |
| `agent/triage/state.py` | Pure state builders, subject cleaning, neutral ids (candidates A–E, events E1–E3, links U1–U4) |
| `agent/triage/questions.py` | The question catalog, `QUESTIONS_VERSION` (bump on any wording change) |
| `agent/triage/features.py` | Pure triage-model features |
| `agent/triage/dates.py` | Urgency buckets → due date; stated deadline parts → due date |
| `agent/triage/policy.py` | Pure decisions: `decide_new_input`, `decide_followup` |
| `agent/triage/fields.py` | Answers → `FieldDraft` (task fields + what the writer must add) |
| `agent/triage/write.py` | The writer: one forced-tool LLM call with only the missing fields |
| `agent/triage/runner.py` | I/O: `new_input`, `followup`, `manual`, `harvest_notes` |
| `agent/triage/hints.py` | `split_hint` for the split gate |
| `services/learn/jev.py` | Triage model (`fit_logistic`, `predict`, `triage_model_for`), `Thresholds`, `thresholds_for`, nightly `refit` |
| `scripts/jev_replay.py` | Offline evaluation; `--csv` per-input dump; `--publish` stores the model and thresholds |

### What runs with Jev off

Every Jev entry point is gated by `jev.configured()` / `jev.acting()`. With
Jev off, the orchestrator, the manual queue, the attribute classifier and
the split gate take exactly their old paths. The nightly `refit` returns at
once when no input carries Jev answers.

Built alongside, and live regardless of Jev:

- **`services/task/build.task_create_from_fields`:** one `TaskCreate`
  builder shared by the new-input agent and the manual queue.
- **`tasks.estimation_source`** (migration `0043`): null for agent
  estimates; the duration group key only changes for non-null sources.
- **Duration observations carry `task_id`.**
- **The attribute classifier takes entry lists** (`classify(entries,
  settings)`).
- **`agent/helpers/precedents.load_task_candidates`**, shared by the agent
  and triage.
- **Small storage helpers:** `raw_inputs.sender_history`,
  `raw_inputs.jev_decided_since`, `tasks.recent_titles`.
- **`SAVE_NOTES_TOOL`.**

## Design reference (as built)

### Decisions

- **The existing agents are the fallback.** They handle everything Jev
  isn't sure about, and everything needing text: calendar events, edits to
  existing tasks.
- **The cosine auto-decide stays the first gate.** Jev covers only the zone
  below `input_dedup_threshold`.
- **Jev reads the input; history goes to the model.** Jev's state has the
  input, candidate tasks (with labels), upcoming events and links. NOT_TASK
  precedents and sender history are model features only.
- **The triage model** is a MAP logistic regression (σ = 2) over the log-odds
  of the averaged task choice and the six signal questions, `directed_at_me`
  (±1, 0 if unknown), source, the sender's smoothed task share and how often
  they were seen, and the highest precedent similarity per status.
- **Thresholds and model are refit nightly** from final statuses (180 days),
  keyed by Jev model and `QUESTIONS_VERSION`:
  - targets: drop 0.98, create 0.90, target 0.95, no_change 0.95;
  - fewer than 50 rows keeps the previous value;
  - a decision that never reaches its target is disabled (1.01).

### Question catalog

One call per input.

| Id | Type | Used for |
|---|---|---|
| `is_task`, `is_task_swapped` | choice, both orders | averaged → model feature (p(task) while no model is fitted) |
| `addressed_to_user`, `asks_for_action`, `automated`, `fyi_only`, `acknowledgement`, `others_act` | noul | model features |
| `target`, `target_action` | choice | match a candidate task / what the input changes about it |
| `has_event`, `has_facts` | noul | route events to the agent / notes-only call |
| `label`, `work_type`, `effort` | choice | fields |
| `estimate` | score, 8 levels (15 … 240+ min) | fields; most probable level |
| `urgency`, `deadline_stakes`, `due_*` parts | choice | implicit and stated due dates |
| `related_event`, `link` | choice | event linking, source link |
| `at_home`, `names_place`, `title_ok`, `needs_description`, `split` | noul | location, writer gates, split gate |

### Flows

- **New input** (after the cosine gate):
  - `not_task` when 1 − p(task) ≥ the drop threshold and no event;
  - `no_change` for a confident match that adds nothing;
  - `close` only with `JEV_CLOSE_ENABLED`;
  - `create` with a confident label, then the writer only for missing
    text;
  - everything else goes to the agent, with `jev` in its trace.
- **Thread follow-up** (single tasks only): nothing_new → `no_change`;
  done → close behind the flag; anything else → agent.
- **Manual creation** (no event cues, no context inputs): Jev fields plus
  the embedding in parallel; the writer only for missing text; fallback to
  the extractor without a confident label.

### Risks if re-enabled

- **Prompt injection:** email text can steer answers. Mitigations: high
  drop threshold, close off, every write reversible.
- **Language:** Jev's accuracy is best in English; check German inputs
  separately.
- **Data location:** inputs go to a US-processed API (no training on
  customer data; ZDR only on enterprise plans).
- **Outage or rate limit:** `JevUnavailable` → the agent path, as before.

## Relation to the ML proposal in `scheduling-plan.md`

The triage model is the proposal's logistic-regression task gate, with
Jev's reads added to the history features. The v2 weights suggest the
history features alone may carry most of it, which brings the original
proposal (no Jev) back into play. Untested follow-ups from the proposal:

- kNN duration from similar closed tasks;
- kNN implicit deadline (median `due − received` of neighbours).
