# Jev triage — implementation plan

**Status (2026-10-26): implemented, off by default, triage questions v2.**
Nothing calls Jev until `TYPESAFE_API_KEY` is set and `JEV_MODE` is `shadow`
or `on`. The first replay (questions v1) failed the gate; v2 redesigns the
task decision and adds a triage model fitted on the user's history — see
"Replay results" and "Rollout".

Jev (TypeSafe AI, `POST https://api.typesafe.ai/v1/systemone`) is a hosted
classifier. It takes a `state` (text or JSON) plus named questions — `choice`
(up to 255 options), `score` (up to 10 ordered levels) or `noul` (yes/no) —
and returns, for every question, a probability for each option. It writes no
text and compares dates as text. It costs $0.042 per million input tokens,
and output is free. TypeSafe's benchmarks put it near Sonnet level, which is
above the `claude-haiku-4-5` that production runs.

The goal: Jev makes every closed decision, and the LLM only writes text
when text is actually needed.

- Classification (task / not task / duplicate / follow-up action) without
  an agent run for the confident majority of inputs.
- Label, work type, effort, estimate and due date from Jev — including
  stated deadlines, which Jev reads as parts and code assembles.
- Titles stay good: the LLM writes them unless the source already has a
  clean one. Descriptions only when Jev says one is needed.
- Manual creation in about a second. Today the P50 is 4.2 s.
- A triage model and thresholds learned from the user's own history.

The hand-off feature (agents doing tasks, email drafts) is a separate plan:
`docs/handoff-plan.md`. It adds questions to the same Jev call.

## Production baseline (2026-10-25)

- Last 60 days: 691 inputs reached the new-input agent; 358 were dropped by
  the cosine auto-decide; 139 were manual extractions; 30 were thread
  follow-ups.
- All time: 4,128 inputs. Gmail and Slack are 86 % `not_task` (3,275 of
  3,793). There are 87 manual overrides and 7 `is_task` corrections in
  `task_overrides`.
- Manual creation: P50 4.2 s from POST to the processed row (last 120 days,
  rows over 10 minutes excluded).

## Decisions and assumptions

- **The existing agents are the fallback.** `run_new_input_agent`,
  `run_thread_followup` and `extract_task_fields` are unchanged. They handle
  everything Jev isn't sure about, and everything needing text Jev can't
  produce (calendar events, edits to existing tasks).
- **The cosine auto-decide stays the first gate.** It is free and
  calibrated. Jev covers the zone below `input_dedup_threshold`, where the
  agent ran before.
- **Precedents go in through the state.** Jev has no few-shot or
  fine-tuning API (same weights for every account). Every state carries the
  ranked candidates the orchestrator already retrieves (with their labels),
  the NOT_TASK precedents with their reasons, and per-sender history (how
  earlier inputs from the same address ended).
- **The adaptation is ours: a triage model.** Jev answers narrow, literal
  yes/no questions. A logistic regression fitted on the user's labelled
  history combines them with sender history, direction and precedent
  similarity into p(task). This is the downstream classical model TypeSafe
  recommends for domain fitting. The policy acts on that probability.
- **Thresholds are option probabilities, not Jev's `confidence`.** The
  scale of `confidence` depends on the number of options; a probability
  means the same on every question.
- **The first option wins close calls** (documented jaggedness). The task
  decision is therefore asked in both orders and averaged. The v1 lean
  towards "task" flooded Slack with false tasks. Other choices put the
  cheaper mistake first: `none` before any candidate, `none` before any
  date part.
- **Explanations are not needed.** A Jev decision stores
  `reason = "jev: not a task"` and `confidence = p` in the trace.
- **The model is pinned** (`jev-1.14.0`, never `jev-latest`). The triage
  model and the thresholds are keyed by Jev version **and**
  `QUESTIONS_VERSION`, so new wording or a new Jev version starts from
  scratch.
- **Every answer is logged** under `agent_trace["jev"]` (model, latency,
  tokens, each answer's option and probability, the decision and why).

## Code map

| Path | Role |
|---|---|
| `agent/helpers/jev.py` | Client (raw httpx): `Choice` / `Score` / `Noul`, `ask`, `JevUnavailable`; one retry on 429/529/5xx; Langfuse span per call |
| `agent/triage/state.py` | Pure state builders, subject cleaning, neutral ids (candidates A–E, events E1–E3, links U1–U4) |
| `agent/triage/questions.py` | The question catalog |
| `agent/triage/dates.py` | Urgency buckets → due date; stated deadline parts → due date |
| `agent/triage/features.py` | Pure triage-model features from Jev's answers and history |
| `agent/triage/policy.py` | Pure decisions: `decide_new_input`, `decide_followup` |
| `agent/triage/fields.py` | Answers → `FieldDraft` (task fields + what the writer must add) |
| `agent/triage/write.py` | The writer: one forced-tool LLM call with only the missing fields |
| `agent/triage/runner.py` | I/O: `new_input`, `followup`, `manual`, `harvest_notes` |
| `agent/triage/hints.py` | `split_hint` for the split gate |
| `services/learn/jev.py` | Triage model (`fit_logistic`, `predict`, `triage_model_for`), `Thresholds`, `thresholds_for`, nightly `refit` |
| `services/task/build.py` | `task_create_from_fields`, shared by both agents, the queue and triage |
| `scripts/jev_replay.py` | Offline evaluation against history; `--publish` bootstraps the model and thresholds |
| `migrations/versions/0043_estimation_source.py` | `tasks.estimation_source` |

## Question catalog

One call per input; all questions share one state.

| Id | Type | Used for |
|---|---|---|
| `is_task`, `is_task_swapped` | choice: task / not_task, in both orders | averaged → triage model feature (and p(task) while no model is fitted) |
| `addressed_to_user`, `asks_for_action`, `automated`, `fyi_only`, `acknowledgement`, `others_act` | noul | triage model features; none decides on its own |
| `target` | choice: none / A–E | match a candidate task (only with candidates) |
| `target_action` | choice: nothing_new / done / changes / revives | matched task or thread follow-up |
| `has_event` | noul | any yes ≥ 0.3 → agent (it creates calendar events) |
| `has_facts` | noul | yes ≥ 0.3 → a notes-only LLM call after the decision |
| `label` | choice: label catalog / none | create |
| `work_type`, `effort` | choice | fields (label defaults below 0.6) |
| `estimate` | score: 15 … 240+ min (8 levels) | fields; most probable level, never interpolated |
| `urgency` | choice: today … no_rush | due date when none is stated |
| `deadline_stakes` | choice: firm / hard | `deadline_kind` when a deadline is stated |
| `due_mode` + `due_month` / `due_day` / `due_year` / `due_anchor` / `due_weekday` / `due_week` / `due_hour` / `due_minute` | choice | stated deadline read as parts (TypeSafe's date-extraction pattern); `dates.assemble_due` does the calendar math |
| `related_event` | choice: none / E1–E3 | link to an upcoming event (only with events) |
| `link` | choice: none / U1–U4 | source link (only with URLs) |
| `at_home`, `names_place` | noul | location `home`, or the writer extracts the place |
| `title_ok`, `needs_description` | noul | writer gates |
| `split` | noul | split gate |

## How each flow uses it

**New input** (`orchestrator` after the cosine gate → `triage.new_input`):

| Decision | Condition (defaults; refit per source) | Action |
|---|---|---|
| `not_task` | 1 − p(task) ≥ 0.97, no event | finalize `not_task` |
| `no_change` | candidate p ≥ 0.9 and nothing_new p ≥ 0.9 | `apply_task_action(no_change)`, finalize `duplicate` |
| `close` | done p ≥ 0.97, **only with `JEV_CLOSE_ENABLED`** | `apply_task_action(update_task, closed)` |
| `create` | p(task) ≥ 0.9, `none` p ≥ 0.9 if candidates, label p ≥ 0.6 | fields → writer (only if needed) → task → split/schedule |
| `llm` | everything else, any event | the agent runs, with `jev` in its trace |

p(task) is the triage model's probability. While no model is fitted for the
current Jev and question version, it is the both-orders `is_task` average.
The trace stores the features, `p_task` and `p_task_source` (`model` /
`choice`).

`has_facts` doesn't block a decision; it adds the notes-only call.

**Thread follow-up** (`orchestrator` thread branch, single tasks only —
containers with subtasks keep the agent): nothing_new → `no_change`; done →
close (behind the flag); anything else → agent.

**Manual creation** (`queue._process`, fresh manual entries without event
cues or context inputs → `triage.manual`):

- **Calls:** the field questions run in parallel with the embedding. A
  second small Jev call matches related events when there are any.
- **No LLM call:** a clean typed title with no stated deadline ("Buy printer
  paper").
- **One small writer call:** the deadline needs a time Jev couldn't read,
  or the title contains it ("Dentist call Friday 3pm").
- **Agent fallback:** no confident label, or the writer fails → today's
  extractor.
- **User fields win:** a user-typed title is never rewritten, and a typed
  estimate is kept.

**Writer** (`write.py`): title (if `title_ok` < 0.9), description (if
`needs_description` ≥ 0.5), due date (stated but unreadable), location
(named place). Title prompts include the 3 latest titles under the same
label as style examples. Runs on the default model.

**Attribute classifier** (calendar events, task backfill): with Jev acting,
one Jev call per entry (other entries would only distract it), falling back
to the Flash batch on `JevUnavailable`.

**Split gate**: an estimate over 120 min still splits. For a conjunction
title ("Tom and Jerry"), Jev's `split` answer replaces the regex when Jev is
acting.

**Durations**: Jev's bucket goes through `with_learned_estimate` like the
agent's guess. `tasks.estimation_source = "jev"` puts it in its own
duration group (`label|work_type|jev`), so each estimator's bias is learned
separately.

## Learning

`services/learn/jev.refit`, run from `jobs.nightly`, over every input from
the last 180 days whose trace carries Jev answers for the current question
version. The label is whether the input ended up linked to a task the user
didn't dismiss: a dismissal flips the task to `not_task`, a promotion links
one.

- **Triage model** (`jev_triage_model`):
  - **What:** MAP logistic regression (Gaussian prior, σ = 2) over the
    stored features.
  - **Features:** log-odds of the averaged task choice and each signal noul;
    `directed_at_me` (±1, 0 if unknown); Slack; the sender's smoothed task
    share and how often they were seen; the highest similarity to a NOT_TASK
    precedent and to a task candidate.
  - **Minimum data:** at least 200 rows and 20 positives; otherwise the
    previous model stays.
- **Thresholds** (`jev_thresholds`):
  - **Which:** per decision and source — `drop`, `create`, `target` and
    `no_change` — each the lowest probability whose answers at or above it
    reach the precision target (drop 0.98, create 0.90, target 0.95,
    no_change 0.95).
  - **Model change:** drop and create are recomputed from the stored
    features with the freshly fitted model, so thresholds follow it.
  - **Not enough data:** with fewer than 50 rows, the previous value stays —
    a replay-bootstrapped one or the default. Sources also pool into `all`.
  - **Never good enough:** a decision that never reaches its target is
    disabled (1.01).
- **Keying:** both are stored per Jev model and `QUESTIONS_VERSION`.

## Replay results

**v1 (2026-10-25, 533 inputs):** failed the gate.

| | Gmail | Slack |
|---|---|---|
| Always "not a task" (trivial baseline) | ~87 % | ~95 % |
| Jev (`is_task`, task first) | 77.6 % | 65.7 % |
| Agent (upper bound: the final outcome is its decision unless corrected) | 93.8 % | 95.0 % |

- **Task bias:** Jev called many non-tasks tasks (create precision 9–24 %).
  The causes are the first-option lean plus wording that made any question
  a task.
- **Drop precision:** 100 % on Slack, but at most 95 % on Gmail. Some of the
  Gmail misses were follow-ups to existing tasks, not new tasks.
- **Labels (51 %):** mostly a grading artifact. 107 of the 297 recent
  tasks carry labels that no longer exist in the catalog, and `Reise` has no
  description.
- **Estimates:** graded only against the agent-shaped final value, which
  says nothing about correctness.

**v2 changes:**

- **Questions:**
  - the task question is asked in both orders and averaged;
  - six narrow signal nouls;
  - candidates carry their labels.
- **Triage model:** fitted on the user's history.
- **Replay report:**
  - outcome mix and the trivial baseline;
  - misses split into new tasks and follow-ups;
  - out-of-fold model scores next to the raw choice;
  - labels graded only against the current catalog;
  - estimates graded against the user's corrections and observed
    durations;
  - the subject in the CSV, for reading disagreements.

## Rollout

1. **Replay** (prod, read-only), with a larger sample since the model
   needs data:
   `docker exec -e TYPESAFE_API_KEY agent-007-app-1 python -m app.scripts.jev_replay --days 180 --limit 2000 --csv /tmp/jev.csv`.
   - **What it does:** rebuilds each input's state from the candidates
     stored in its trace (no leakage from later inputs) and fits the triage
     model out-of-fold.
   - **What it prints:** the report above.
   - **Read the disagreements:** go through 20 rows of the CSV, because the
     ground truth is partly the agent's own decision.
   - **Gate:** the out-of-fold model must reach the drop target (98 %) at a
     useful coverage, and create precision must beat the agent. Otherwise
     iterate the questions (bump `QUESTIONS_VERSION`) and re-run; a run
     costs well under a dollar.
2. **Bootstrap**: once the gate holds, re-run with `--publish` and without
   the read-only `PGOPTIONS`. That stores the model and the out-of-fold
   thresholds; the nightly refit replaces them only once live traces have
   enough rows.
3. **Shadow**: set `JEV_MODE=shadow` in prod for about a week. Every input
   gets Jev's answers in its trace, and the agents still decide. The
   nightly refit now has rows from all of them.
4. **On**: set `JEV_MODE=on`. `not_task`, `no_change` and `create` act
   above the thresholds. Manual entries take the fast path.
5. **Close**: turn on `JEV_CLOSE_ENABLED=true` only after the replay and
   shadow rows show `done` answers at ≥ 0.97 are reliable.

Migration `0043_estimation_source` runs with the normal deploy
(`alembic upgrade heads`).

## Risks

- **Prompt injection:** email text can steer answers. Mitigations:
  - drop needs p ≥ 0.97 and a 0.98 precision target;
  - close is off by default;
  - every write is reversible (dismiss, reopen, promote).
- **Language:** Jev's accuracy is best in English. German inputs need
  their own look in the replay, and their own thresholds if they lag
  (split by source today; add a language split if needed).
- **Calibration off-distribution:** per-source thresholds; the nightly
  refit disables a decision that stops meeting its target.
- **Version drift:** pinned model, thresholds keyed by version.
- **Outage or rate limit:** `JevUnavailable` → the agent path, as before.
- **Data location:** inputs go to a US-processed API (no training on
  customer data; ZDR only on enterprise plans).

## Relation to the ML proposal in `scheduling-plan.md`

The triage model *is* the proposal's logistic-regression task gate. It uses
Jev's reads, sender history and precedent similarity as features instead of
the raw embedding. Jev's label question replaces the kNN label hints;
candidate labels in the state carry the neighbour signal. Two follow-ups
stay:

- kNN duration (median observed duration of similar closed tasks) instead
  of the Jev bucket when ≥ 10 neighbours have observations.
- kNN implicit deadline (median `due − received` of neighbours) instead of
  the urgency table.

Both go through the same replay ship rule, with their own `estimation_source`.
