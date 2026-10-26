# Jev triage — parked

**Status (2026-10-26): parked. Production stays on the Haiku agents.**

The Jev integration is in the repo and off: `JEV_MODE=off`, no
`TYPESAFE_API_KEY`. With Jev off, every flow behaves as before (see "What
runs with Jev off"). Two replays against production history showed Jev is
only good enough for one decision — dropping obvious non-tasks — and that
decision saves too little to justify the vendor, the model and the nightly
refit right now. This document keeps what was built, what was measured,
what to do if the idea comes back, and what to clean up once Jev runs.

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

## The ML proposal Jev grew out of

Before Jev, the scheduling plan proposed learning the agent's closed
decisions from the user's corrections (`task_overrides`, check-ins,
calendar resizes). The scheduling plan is gone; this is what's left of the
proposal:

| Decision | Proposal | Status |
|---|---|---|
| Task vs not task | Logistic regression on the input embedding plus source features; auto-drop below p < 0.03, otherwise a hint in the prompt | Built as the triage model above, with Jev's reads added. Parked with Jev. The v2 weights suggest the history features alone carry most of it — the history-only ablation in "Next experiment" tests exactly that |
| Label | Recency-weighted kNN over task embeddings, as a prompt hint | Not built. The agent's labels are rarely corrected (4 UI corrections, ever) |
| Duration | LLM estimate × learned correction, plus a kNN residual from similar closed tasks | The learned correction is live (`services/learn/jobs.refit_duration`, `duration_params`). The kNN residual is untested |
| Deadline, explicit | Stays LLM extraction | — |
| Deadline, implicit | Median of (final due − received) over kNN neighbours per label and source; replaces the guess when ≥ 10 neighbours agree | Untested |
| Work type / effort | kNN like the label | Not built |

A model ships only if it beats the agent on held-out recent data (accuracy
for task and label, MAE in log-minutes for duration, MAE in hours for
deadlines). The live metric is the override rate per field. Fitting lives in
`app/services/learn/`, parameters in `learned_models`, and predictions reach
the agent as prompt context — no new agent tool.

## Cleanup once Jev is in production

Moved here from the old cleanup plan: everything below assumes Jev is on
and has proven itself, so none of it applies while Jev is parked. Part B of
that plan (the legacy scheduler) is done.

State on 2026-10-26: Jev off (`JEV_MODE=off`, no key). The agents run on
Anthropic (`CLAUDE_MODEL=claude-haiku-4-5` in prod); chat, the subtask
planner and link research already run on `gemini-3.8-flash`.

### Exit criteria (all must hold before any step)

1. `JEV_MODE=on` in prod for at least 4 weeks.
2. `jev_thresholds` is fitted for gmail and slack (≥ 50 rows each), not
   running on defaults.
3. The corrections on Jev-decided inputs are no higher than they were on
   agent-decided ones, over the same 4 weeks:
   - dismissals of Jev-created tasks;
   - promotions of Jev `not_task` inputs;
   - `task_overrides` on label and estimation.
4. At most one dropped real task (a promotion of a `jev_not_task` input)
   per month.

The replay script (`python -m app.scripts.jev_replay`) and the `jev_*`
branches in `agent_trace` give all four numbers.

### 1 — Widen Jev, then retire the regex cues it replaces

Small behaviour changes that let the later deletions happen:

- **Manual event cue.** Ask `has_event` in the manual call and route on it,
  then delete the `mentions_event` regex check in `queue._process`. The
  regex itself in `services/event_context.py` stays only if another caller
  remains.
- **Promotions.** Also run the manual fast path for promoted Gmail/Slack
  inputs (`POST /tasks/open/{id}`) that have no candidates and no context
  inputs. `extract_task_fields` then only handles candidate updates, event
  cues and multi-message threads.
- **kotx briefs.** `agent/kotx/runner.py` runs `extract_task_fields` only
  for estimation and due date. Replace it with the Jev field call (no
  writer — the title comes from the GitHub subject). One LLM call fewer per
  kotx task.
- **Split cue.** Remove `_CONJUNCTION` from `services/task/split.py`.
  `needs_split` keeps the over-120-minute rule; a conjunction no longer
  matters without Jev's `split` answer.

### 2 — Delete what Jev made redundant

| What | Where | Why it can go |
|---|---|---|
| Flash batch path of the attribute classifier | `services/attribute_classifier.py`: `numbered`, `tool`, `parse`, `MAX_TOKENS`, the `chat` branch | A Jev outage leaves events/tasks pending; `classify_pending_events` and the backfill retry them next run. Also drop `tool_name`, `description`, `items` and `system_prompt` from `BatchClassifier` and its two definitions, plus the `CLASSIFIER.parse` / `.tool` tests |
| Cosine `auto_not_task` | `orchestrator.py`, `input_dedup_threshold` for the not-task side | **Decide on replay data first.** Compare Jev with the cosine gate on the `auto_not_task` rows. If Jev agrees ≥ 99 %, let Jev decide those too and keep only `auto_duplicate`. That removes a second, uncalibrated drop path |
| `confidence` on the agent tools | `agent/tools/schemas.py` `_CONFIDENCE_SCHEMA`, its uses, `trace["confidence"]` writes | Jev's probability is the calibrated number; the LLM's self-reported confidence isn't used for anything. Keep `reason` |
| Classification guidance in the agent prompt | `agent/prompts/new_input.py` | The agent then only sees hard cases (events, uncertain inputs, edits). Shorten the "is it a task" section to what those need — as a prompt edit plus replay, not a blind cut |
| Unused override readers | `db/clients/overrides.py` `list_for_field`, `task_ids_with_user_override` | No callers. They were prep for the label/deadline kNN above; delete them if those stay unbuilt |

### 3 — Run every remaining LLM call on Gemini Flash

After steps 1–2 the LLM does two kinds of work:

- **Writing text:** the triage writer, the notes harvest, note moderation
  and subtask plans.
- **Hard cases:** calendar events, edits to existing tasks, and inputs Jev
  isn't sure about.

Chat already runs on `gemini-3.8-flash` with tools, forced tool calls and
thought signatures, so the Google path through `agent/helpers/llm.py` is
proven. One provider removes the Anthropic SDK, its key, and the
two-provider branches in the adapter.

| Call site | Provider in prod today |
|---|---|
| New-input agent (`agent/input/runner.py`), thread follow-up (`agent/thread/runner.py`), manual extractor (`agent/manual/runner.py`, also kotx briefs until step 1) | default → Anthropic `claude-haiku-4-5` |
| Triage writer (`agent/triage/write.py`), notes harvest (`agent/triage/runner.py`) | default → Anthropic |
| Note moderation (`services/note/moderate.py`) | default → Anthropic |
| Subtask planner (`services/task/split.py`) | Flash (whenever `GEMINI_API_KEY` is set) |
| Attribute classifier fallback | Flash (deleted in step 2) |
| Chat (`agent/chat/runner.py`) | Flash (`chat_llm_*`) |
| Link research (`agent/helpers/web.py`) | Flash with Search grounding; replaced by the hand-off research executor (`docs/handoff-plan.md`, W-steps) |

Steps:

1. **Prepare the Google path for the agent loops** (code, before the
   switch):
   - **Thinking level:** `chat()` gets a default thinking level for Google —
     a new setting `llm_thinking_level` (default `low`), used whenever the
     caller passes none. The agents never pass one, so on Gemini they would
     run the model's default thinking.
   - **Output cap:** raise `MAX_TOKENS` on the Google path. Thinking tokens
     count against `max_output_tokens`; that's why the attribute classifier
     already needs 4096. Agent turns capped at 1024 would truncate.
   - **Tool schemas:** test each flow's tools on Gemini (new-input, thread,
     manual, writer, notes, moderation, split). Gemini accepts a subset of
     JSON Schema; `format: uuid`, `minLength` and `minItems` are the keys to
     check.
   - **How:** a unit test per flow that converts the tools with
     `_convert_tools_to_google_genai_format`, plus one live call per flow on
     a recent input.
2. **Switch by env** (reversible): set `LLM_PROVIDER=google` and
   `LLM_MODEL=gemini-3.8-flash`, and remove `CLAUDE_MODEL`. Rollback means
   restoring the old values.
3. **Gate** (2 weeks), measured on agent-decided inputs only (`new_input`,
   `thread_followup` and `manual` branches) and compared with the last Jev
   weeks on Haiku:
   - the same corrections as the exit criteria;
   - `no_tool_call` and `max_iterations` outcomes;
   - writer failures (`jev triage · writer failed` in the logs);
   - title quality, now written by Flash: spot-check 20.
4. **Delete Anthropic** once the gate holds:
   - **Adapter** (`agent/helpers/llm.py`): the `AnthropicChatGenerator`
     import, `_anthropic_generator`, `CACHE_CONTROL` and the cache marker in
     `_to_haystack_messages`, and the Anthropic branches of
     `_build_generator`, `_generation_kwargs` and `_tool_choice`. Gemini's
     own context caching covers the repeated system/tool prefix.
   - **Routing that becomes constant:** `provider="google" if … else None`
     in `services/task/split.py` and `services/attribute_classifier.py`;
     `provider="google"` in `agent/helpers/web.py`; `Settings.llm_target`
     and the `chat_llm_provider` / `chat_llm_model` pair. Chat keeps
     `chat_thinking_level` if it should think differently from the
     background flows.
   - **Settings:** remove `anthropic_api_key`, `claude_model` and
     `DEFAULT_ANTHROPIC_MODEL`. `llm_provider` goes or becomes google-only.
     The `llm_model` default becomes `gemini-3.8-flash`.
   - **Dependencies:** `anthropic-haystack` and `anthropic` in
     `pyproject.toml`, plus the `MessageParam` pin comment; then `uv lock`.
   - **Config:** the LLM section of `.env.example`; `ANTHROPIC_API_KEY` and
     `CLAUDE_MODEL` in the prod env.
   - **Comments:** the `observability.py` module docstring (it names
     `AnthropicChatGenerator`), the settings comment about the "cached
     Anthropic path", and the `_generation_kwargs` docstring.

### 4 — Consider

- **Default `jev_mode` to `on`.** Keep the setting itself: `off` is the
  kill switch.

### Tests to update

- `test_attribute_classifier_jev.py`: the fallback test becomes "outage
  leaves the batch pending".
- The parse/tool tests in `test_calendar_event_attributes.py` and
  `test_backfill_task_attributes.py`.
- `test_task_split.py` conjunction tests.
- `test_manual_task_creation.py` event-cue tests.
- `test_kotx_consolidation.py` extract-fields stubs.
- The Anthropic cases in `test_llm_adapter.py` and `test_llm_tracing.py`
  (step 3). Fake responses elsewhere that only label themselves
  `provider="anthropic"` can stay or be relabelled.
