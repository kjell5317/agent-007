# Preference-based scheduling — implementation plan

Replace the rule-driven greedy planner in `app/services/plan/schedule.py`
with a global solver (OR-Tools CP-SAT) whose objective is a learned
preference model. Hard constraints shrink to what is physically or
logically required; everything the old planner encoded as rules (day
windows, buffers, victim order, extended-window fallback) becomes the
*prior* of the preference model, so day one behaves like today and drifts
toward observed behaviour as data accrues.

Two phases:

- **Phase 1** — data capture, end-of-slot check-ins, work type / effort,
  deadline kinds, solver with rule prior, continuous learning with
  posterior sampling. Commute is not modelled in the solver.
- **Phase 2** — commute inside the solver, work type / effort for calendar
  events, multi-user.

A separate section at the end proposes where ML can take over or assist
decisions in the extraction agent.

## Decisions and assumptions

| Topic | Decision |
|---|---|
| Time grid | 5 minutes. Durations and commute legs round **up** to 5 min. |
| Solver | OR-Tools CP-SAT (new dependency). 10 s limit per trigger, 60 s nightly, warm-started from the current calendar. Result = best schedule found within the limit, never worse than the current one; optimality is not proven at realistic sizes (see *Solver limits and horizon*). |
| Horizon | 14 days. Tasks due later may be *deferred*: not placed now, reconsidered on every solve (step 7). |
| Hard: overlap | No two tasks overlap; no task overlaps a calendar event. |
| Hard: events | Calendar events not owned by the app never move. |
| Hard | Nothing starts in the past. `depends_on_task_id` order holds. A repeat occurrence doesn't start before its period. A task whose slot has started stays fixed, including a running *Still working* extension. |
| Deadline | Per task: `hard`, `firm` or `soft` (step 3, step 6). Lateness is penalised by kind; an overdue task gets placed instead of failing. |
| User-placed tasks | Movable. A strong anchor while their surroundings are unchanged; normal stability once the surroundings change (step 7). |
| Removed | Day windows (10–19, 8–24), the 15-min event buffer, the same-day rule, `LEAD_DAYS`, victim ordering, displacement repair. All become prior weights. |
| Commute (phase 1) | Not in the solver. The commute planner still derives legs after placement. Legs are derived output, not busy input. |
| Points | The "−N points, slot missed" notification goes away. The deduction itself stays on *Not done* and on no answer (confirm). |
| Stability | Moving an already-placed task costs a fixed amount (not learned), so a new task doesn't reshuffle the week. |

## Phase 1

### Step 1 — Data model (migration `0038_scheduling_learning`)

`tasks`

| Column | Notes |
|---|---|
| `work_type` | `physical` \| `cognitive` \| `social`, nullable |
| `effort` | `low` \| `medium` \| `high`, nullable |
| `deadline_kind` | `hard` \| `firm` \| `soft`, default `firm` |
| `placed_by` | `planner` \| `user`, default `planner` |
| `placed_at` | timestamptz; when the current slot was set |
| `placed_context` | jsonb, user placements only: ids and spans of items within ±2 h and the day load at placement time |
| `running_since` | timestamptz, set while a *Still working* extension runs |
| `schedule_state` | `placed` \| `deferred` \| `unplaced`; `scheduled_date` is null for the last two |

`labels` gains `default_work_type`, `default_effort` (nullable).

`schedule_log` — one row per scheduling-relevant event; the training set.

| Column | Notes |
|---|---|
| `id` | uuid pk |
| `task_id` | fk tasks, indexed |
| `kind` | `placed` \| `moved_by_user` \| `resized_by_user` \| `checkin_sent` \| `still_working` \| `not_done` \| `done_confirmed` \| `closed` \| `no_response` \| `abandoned` \| `deferred` \| `unplaced` |
| `actor` | `planner` \| `user` \| `checkin` \| `system` |
| `slot_start`, `slot_end` | the slot the row is about |
| `prev_start`, `prev_end` | previous slot for moves / resizes |
| `solver_run_id` | uuid, nullable; groups placements of one solve |
| `features` | jsonb snapshot of the feature vector(s) at that moment (`{"slot": {...}, "prev": {...}}`). Neighbours change later, so features can't be recomputed afterwards. |
| `created_at` | |

`task_overrides` — user corrections of agent output.

| Column | Notes |
|---|---|
| `id`, `task_id`, `raw_input_id` | |
| `field` | `due_date` \| `deadline_kind` \| `estimation` \| `label` \| `work_type` \| `effort` \| `is_task` |
| `agent_value`, `user_value` | jsonb |
| `source` | `ui` \| `calendar` \| `notification` \| `backfill` |
| `created_at` | |

`learned_models` — versioned fitted parameters.

| Column | Notes |
|---|---|
| `id`, `kind` | `slot_preference` \| `duration` \| `is_task` \| `label` \| `deadline` |
| `version`, `params` (jsonb), `n_samples`, `metrics` (jsonb) | For `slot_preference`, `params` holds the MAP weights and the posterior covariance. |
| `active` | one active row per kind |
| `fitted_at` | |

Storage functions live in `app/db/clients/schedule_log.py`,
`overrides.py`, `learned_models.py`.

### Step 2 — Capture overrides and manual moves

Record at the service layer with an explicit `actor`, never in routers,
so agent follow-up edits (a new email moving a deadline) are not counted
as user overrides.

| Where | What gets recorded |
|---|---|
| `update_task_svc` called from `PATCH /tasks/{id}` | `task_overrides` for changed `due_date` / `estimation` / `label` / `work_type` / `effort` (`source=ui`). A changed `scheduled_date` → `schedule_log moved_by_user`, `placed_by=user`. |
| `_sync_task_schedule_from_event` in `services/calendar/discover.py` | It already only writes when the event differs from what the app pushed, so any diff is a user edit. Start change → `moved_by_user`; duration change → `resized_by_user` + `task_overrides estimation (source=calendar)`. Both set `placed_by=user`. |
| `mark_not_task` (`api/tasks.py`) | `task_overrides is_task: true → false` |
| `open_task_from_input` (not_task input opened) | `task_overrides is_task: false → true` |
| Every solver placement | `schedule_log placed` with features and `solver_run_id` |

Backfill: a one-off script compares each task's agent tool-call input in
`raw_inputs.agent_trace.iterations[].blocks[].input` with the current task
fields and writes `source=backfill` rows. Noisy (calendar resizes and
agent updates also changed fields), so learning down-weights them.

### Step 3 — Work type, effort and deadline kind

- Add `work_type` / `effort` / `deadline_kind` enums to `_CREATE_TASK_PROPS` and
  `_UPDATE_TASK_PROPS` in `app/agent/tools/schemas.py`; pass them through
  in the input, manual, chat and kotx runners.
- Prompt (`agent/prompts/new_input.py`): render each label with its
  defaults ("Sport — physical, medium") and instruct the agent to inherit
  them unless the task clearly differs.
- Deadline kind rules in the prompt: `hard` when missing the deadline
  makes the task pointless or has external consequences (submissions,
  exams, appointments, preparation for a related event); `firm` for other
  explicit deadlines; `soft` for derived deadlines (`due_date_derived`) and
  "sometime" phrasing. The kind is a user-editable field like the others,
  and later an ML hint (see the last section).
- Labels API (`api/labels.py`) and UI: edit the defaults.
- Task detail UI: show and edit all three fields (edits feed `task_overrides`).
- Backfill script for open tasks and tasks from the last 180 days, one
  batched call per 20 tasks on the cheap model.

### Step 4 — Holiday calendar

- Setting `google_holiday_calendar_id` (e.g. the German public-holiday
  calendar) plus `.env.example`.
- `services/calendar/holidays.py`: fetch all-day events for the horizon,
  return a `set[date]`, cache per process for 24 h. Not added to busy
  calendars — holidays are a feature, not a blocker.

### Step 5 — End-of-slot check-in (replaces the penalty notification)

Hook: `reschedule_overdue_scheduled_tasks_once` in `app/cron.py` already
fires at slot end + 15 min (`OVERDUE_TASK_GRACE`). It becomes
`checkin_overdue_slots_once`:

1. For each open task whose slot ended ≥ 15 min ago with no
   `checkin_sent` row for that slot: send the notification, log
   `checkin_sent`. Don't reschedule yet.
2. Notification: tag `task-<id>` (replaces the creation notification),
   title "Still on *<title>*?", three actions (HA companion and ntfy both
   cap at three):

| Action | Button | Effect |
|---|---|---|
| `CHECKIN_DONE` | Done | `close_task` with completion time = slot end; log `done_confirmed`. |
| `CHECKIN_STILL_WORKING` | Still working | Log `still_working`, set `running_since`. No further notification. |
| `CHECKIN_NOT_DONE` | Not done | Log `not_done`; deduct penalty points silently; re-solve. |

3. No answer within 2 h: log `no_response`, deduct points, re-solve.
   Excluded from learning (ambiguous).
4. Closing a task in the app logs `closed` with the close time.
5. Running tasks: a cron extends the calendar event by 15 min every
   15 min until the task is marked done in the app, where a running task
   shows a Done button. Marking done trims the event to the done time.
   The extension stops short of the next calendar event, since events
   never move; time is still counted. A movable task in the way is
   re-solved only when the extension reaches it, not every 15 min.
6. A running task not marked done after 2× its planned duration stops
   extending and is logged `abandoned`: excluded from duration learning
   and re-solved. No notification and no midnight cut-off.

Remove `notify_points_penalty` from the slot path; keep
`subtract_scheduled_overdue_penalty`. Action routing goes into
`api/notifications.py` next to `ACTION_CLOSE_TASK`.

Duration observations derived from the log (assumes the task started on
time; good enough for a correction factor):

| Log pattern | Observation |
|---|---|
| `done_confirmed`, or `closed` inside the slot | actual ≤ planned (upper bound: close − start) |
| `still_working`, then marked done | actual = done time − slot start |
| `not_done` | missed slot, no duration |
| `no_response`, `abandoned` | excluded |

### Step 6 — Feature extraction (pure)

`app/services/plan/features.py`. Input: a `SlotContext` dataclass (task
attributes, slot start/end, previous and next neighbour with kind / label
/ work type / effort / gap, day load, holiday set). Output: sparse
`dict[str, float]`. The same function produces log snapshots and solver
cost tables, so training and serving can't drift apart.

Utility units are log-odds; prior values below are the starting weights
that approximate today's rules.

| Feature | Encoding | Prior |
|---|---|---|
| Hour of day | one-hot, 24 | 10–19: 0 · 8–10 and 19–24: −2 · 0–8: −8 |
| Hour × work type, hour × effort | one-hot | 0 |
| Weekday | one-hot, 7; × work type | 0 |
| Holiday | flag; × work type | 0 |
| Season | sin / cos of day of year | 0, very tight regularisation |
| Gap before / after | buckets 0, 5, 10, ≥15 min | −3, −2, −1, 0 (replaces the 15-min buffer) |
| Neighbour before / after | kind (`none` if gap > 60 min, `event`, `task`) × same-label flag; for tasks also × work type × effort pair. Events carry label only in phase 1. | 0 |
| Lateness | late flag plus hours past the deadline, linear, both per deadline kind | hard: flag −1000, effectively forbidden while any on-time slot exists · firm: flag −10, −0.5 / h · soft: no flag, −0.05 / h |
| Hours to deadline | buckets 0–6, 6–24, 24–72, >72 | 0–6: −1 · rest 0 |
| Days from now | linear | −0.1 / day (today's planner prefers earlier days) |
| Day load | booked minutes that day, buckets <240, 240–360, 360–480, >480 | 0, 0, −1, −3 |

No caps on learned weights. The prior is a starting point, not a limit:
an hour with no observations stays at its prior, and every observation
can move it. How fast it moves is set by the prior's spread per feature
group (step 8).

### Step 7 — Solver

`app/services/plan/solver.py`, pure: `SolveInput` (tasks with duration,
earliest start, fixed flag, predecessor, current slot, static cost table;
fixed intervals; day load base; weights) → `SolveResult` (start or
unplaced per task, objective, bound, wall time).

Model (time unit = 5 min, `t = 0` at horizon start):

- Per movable task: `start ∈ [earliest, H − dur]`, interval of length
  `dur`, presence literal.
- Tasks due inside the horizon, or overdue, must be placed; unplaced
  costs 10⁶ and only happens when nothing fits.
- Tasks due after the horizon may be **deferred** (presence false): no
  slot, no calendar event, `schedule_state = deferred`, reconsidered on
  every solve. Deferring has a value, so the solver weighs "this slot
  now" against "later":
  - Deferral value = the model's utility of a representative good slot
    on day 15 for this task (mean of its 3 best hours, deadline features
    evaluated at day 15), minus a risk term that grows as the capacity
    left before its deadline shrinks.
  - Capacity constraints keep deferral honest. For each week boundary `k`
    after the horizon, up to the latest deadline:
    `Σ dur_i · deferred_i over tasks with due_i ≤ k  ≤  ρ · free_k`.
    `free_k` is the free time in good hours between the horizon end and
    `k`, from coarse busy data (Calendar `freeBusy`); `ρ` is the share of
    free time that historically goes to tasks (prior 0.5, learned in
    step 8). One linear constraint per week, almost free to solve.
  - Effect: a task due in two months with only poor slots this fortnight
    waits; one whose remaining weeks are already full is pulled in now.
- `AddNoOverlap` over task intervals and fixed intervals (fixed tasks,
  calendar events). Gaps are soft (features), not padding.
- Costs are built from small tables, never one entry per start:
  `day_i = start_i // 288`, `tod_i = start_i % 288`, `hour_i = tod_i // 12`.
  Hour terms (hour, hour × work type, hour × effort) fold into a 24-entry
  table per task, day terms (weekday, holiday, season, days from now and
  their work-type interactions) into one entry per horizon day, lateness
  is linear via `AddMaxEquality`. A prototype with one 4032-entry table
  per task spent 25 s in presolve for 40 tasks; the compact form accepts
  the warm start in under 0.5 s.
- Gap and neighbour terms need one yes/no variable per (task, item it
  could sit next to) pair: "ends 0 / 5 / 10 min before", "starts 0 / 5 /
  10 min after", "adjacent within 60 min". Generated for all 14 days by
  default. Fallback when there are more than about 50 movable tasks, or
  shadow-mode solves don't settle within the limit: candidate days only
  (the task's current day plus its 3 best days by the other cost terms).
  A task can still land on any day; on a non-candidate day it is scored
  as having no neighbours.
- The warm-start hint must set these variables too. In the prototype an
  incomplete hint was ignored and the first solution came out far
  worse.
- Task–task neighbours: for pairs whose windows can meet, literal
  `adj_ij ⇒ end_i ≤ start_j ≤ end_i + 12` (60 min); its cost is the pair
  term minus the "no neighbour" term the static table assumed. Pairs
  whose windows can't meet get no literal.
- Day load: `day_i = start_i // 288`, per-day load = fixed minutes + Σ
  durations of tasks on that day; convex penalty through bucket excess
  variables.
- Predecessors: `start_j ≥ end_i`.
- Stability: moving a planner-placed task costs 1; 3 if its current
  slot is within 24 h. A user-placed task costs 20 to move while its
  surroundings match `placed_context`. Once an item within ±2 h appears,
  disappears or moves, or the day load shifts by more than an hour, it
  falls back to the planner cost — the user would likely have moved it
  too.
- `AddHint` from the current calendar, so the result is never worse than
  the current schedule. `num_workers=2`, fixed `random_seed`; tests use
  one worker.
- Durations use the duration correction from step 8 once it exists.

Integration:

- `schedule_task(session, task, ...)` keeps its signature (≈20 call
  sites). Internally it marks the user's schedule dirty and runs
  `replan()` under the existing `_schedule_lock`; a burst of triggers
  coalesces into at most one follow-up solve.
- `replan()`: fetch busy events and holidays, build `SolveInput`, solve,
  diff, patch only moved task events, delete events of newly deferred
  tasks, log `placed` / `deferred`, then run the commute planner for the
  affected days. `retry_unscheduled_tasks_once` skips deferred tasks.
- Setting `scheduler_engine = legacy | shadow | solver`. In `shadow` the
  legacy planner stays authoritative and the solver runs alongside;
  both placements plus objective, bound and wall time go to Langfuse via
  `app/observability.py`. Run one week in shadow before switching.
- After the switch, delete the piggyback, chain-insert, sweep,
  displacement-repair and extended-window code in `schedule.py`. Keep
  what the commute planner imports (leg estimation helpers).

### Solver limits and horizon

Prototype on synthetic weeks: 2 workers (the server's vCPUs), 30 s limit,
5-min grid, compact formulation, warm start from a greedy schedule,
objective = hour prior + days from now + lateness. No neighbour or load
terms, so real models will be slower.

| Tasks | Horizon | First solution | Improvement over warm start | Optimality proven |
|---|---|---|---|---|
| 40 | 14 d | 0.1 s | 30 % | No |
| 40 | 28 d | 0.1 s | 33 % | No |
| 80 | 28 d | 0.3 s | 19 % | No |
| 80 | 56 d | 0.4 s | 21 % | No |
| 150 | 90 d | 1.1 s | 18 % | No |
| 300 | 180 d | 4.1 s | 5 % | No |

With 8 workers, 40 tasks over 14 days reached the same best schedule in
6.5 s instead of 26 s, but the gap only closed to 56 % after 60 s.

Neighbour terms, 14 days, 2 workers, 10 s limit:

| Tasks | Neighbour terms | Extra variables | First solution |
|---|---|---|---|
| 40 | none | 0 | 0.2 s |
| 40 | candidate days | ≈ 5 k | 0.5 s |
| 40 | all days | ≈ 10 k | 2.2 s |
| 80 | candidate days | ≈ 18 k | 2.0 s |
| 80 | all days | ≈ 28 k | 8.3 s |

At 40 tasks the full model fits the 10 s budget; at 80 it doesn't on
2 vCPUs. Hence the fallback threshold in step 7. "Global optimum" in practice means: the best schedule found in the
time limit, never worse than the current one, with the remaining gap
logged.

Limitations:

- **Proof of optimality** is out of reach at these sizes on 2 vCPUs.
- **Linear integer objective.** Weights become integers (log-odds × 100).
  Any interaction needs its own table or extra booleans; pairwise terms
  grow with the square of nearby items. Arbitrary per-slot scores, such
  as a neural network, don't fit well.
- **Formulation is sensitive.** Large element tables blow up presolve;
  keep every table small.
- **Deterministic.** No uncertainty about durations or tasks that haven't
  arrived yet; re-solving on change stands in for it.
- **Determinism of runs** only with one worker; tests use one worker and
  a fixed seed.
- **External inputs are precomputed.** Travel times (phase 2) come in as
  numbers, not live lookups.

Horizon: 14 days. The solver handled 180 days in tests, but far
placements get re-solved many times before they happen and each move is
a calendar write. Deferral covers later deadlines.

### Step 8 — Learning

`app/services/learn/` with pure fitting code plus a nightly cron job.

Slot preference model:

- Pairwise rows from `moved_by_user`: `x = φ(to) − φ(from)`, `y = 1`.
- Pointwise rows from check-ins: `φ(slot)`, `y = 1` for
  `done_confirmed` / closed in slot, `y = 0` for `not_done`.
- Logistic regression, MAP with a Gaussian prior centred on the prior
  weights (L2 toward the prior, σ per feature group). IRLS in numpy,
  no new dependency.
- Sample weight `0.5 ^ (age / 90 days)`; backfill rows × 0.3.
- No minimum-data gate: with little data the posterior equals the
  prior, so the model is used from day one. Refit after each new
  labelled event (milliseconds at this data size), full refit nightly.
- Posterior via Laplace approximation: MAP weights plus the covariance
  from the IRLS Hessian, stored in `learned_models.params`.
- Exploration by Thompson sampling: once per day, draw one weight vector
  from the posterior and use it for every solve that day, so the
  schedule doesn't flicker between triggers. Well-observed features
  barely vary between draws; rarely observed ones (night hours,
  holidays) vary more, so the solver tries them now and then.
  Exploration fades on its own as evidence accumulates. The prior spread
  per feature group sets how adventurous early draws are.
- The held-out log-likelihood on the most recent 20 % of data is
  tracked as a metric, not used as a gate.
- `ρ` for deferral capacity: task minutes completed per week divided by
  free minutes in good hours that week, smoothed over the last 8 weeks.

Duration correction:

- `log(actual) = a[label, work_type] + b · log(estimate)`, fitted on
  exact observations, with upper-bound observations handled as censored
  (only penalise predictions above the bound). Shrinks toward
  `a = 0, b = 1` with little data.
- The solver uses the corrected duration; the UI shows both.

Metrics, weekly, via Langfuse and `GET /stats/scheduling`: share of
slots completed in slot, user moves per week, *Not done* rate,
no-response rate.

### Tests

- `features`: fixture contexts → expected sparse vectors.
- `solver`: small deterministic instances — no overlap, events never
  move, predecessors respected, lateness only when unavoidable, prior
  puts a task inside 10–19 when free, user-placed tasks move only after
  their surroundings change, stability prevents gratuitous moves, a hard
  deadline is never missed while an on-time slot exists.
- Deferral: a far-deadline task waits when only poor slots are free and
  is pulled in when the capacity constraints say later weeks are full;
  tasks due inside the horizon are never deferred.
- `learn`: synthetic logs with a planted preference are recovered;
  with no data the weights equal the prior; posterior draws narrow as
  data grows.
- Check-in: each action produces the right log rows, close / re-solve
  calls and points; a running task extends every 15 min, stops before
  calendar events, and is abandoned after its cap.
- Legacy planner tests (`test_trip_block.py`, parts of
  `test_scheduled_date.py` and `test_recurrence.py`) are retired with
  the legacy code; commute leg tests stay.

### Rollout order

1. Migration, override capture, schedule log. No behaviour change.
2. Work type / effort extraction, label defaults, backfill.
3. Check-in notifications replace penalty notifications.
4. Holiday calendar, feature extraction, feature snapshots in the log.
5. Solver in shadow mode for one week.
6. Switch to solver; delete legacy planner code.
7. Learning with posterior sampling: slot preferences and duration
   correction.
8. Agent ML priors (see below).

Steps 1–4 start collecting data immediately and don't depend on the
solver.

### Risks

- **Commute ignored in phase 1.** A located task can land where travel
  doesn't fit. The commute planner flags such legs; the user's fix
  becomes training data. Accepted regression until phase 2.
- **Unusual placements from exploration.** A daily draw can favour a
  rarely seen hour, such as late night. Moving the task is the
  correction and becomes data; the prior spread limits how often it
  happens.
- **Feedback loop.** The model mostly sees outcomes of slots it chose.
  Posterior sampling and manual moves counter this.
- **Calendar churn.** Stability cost plus patching only moved events.
- **Deferral misjudges future load.** Tasks that haven't arrived yet
  aren't in the capacity constraints; `ρ` absorbs the typical amount of
  new work.
- **Solve time on 2 vCPU.** Warm start, time limit, gap logged; the
  nightly run tightens. Neighbour terms are the main cost driver.

## Phase 2

- **Commute in the solver.** Travel time matrix between home and the
  distinct locations from the route cache, rounded up to 5 min.
  Optional travel intervals adjacent to located tasks; transition
  literals between located tasks on the same day for chaining; the
  home-layover rule as a choice between "chain" and "go home". The
  existing commute planner keeps materialising legs.
- **Work type / effort for calendar events.** Rules first (attendees or
  video link → social; label defaults), a small model for the rest, run
  when the event cache ingests changed events; stored in
  `documents.metadata`.

## ML in the agent's decisions — proposal

Today the LLM decides everything. A precedent shortcut already
auto-decides near-identical inputs by embedding similarity, raw inputs
carry embeddings, and `agent_trace` keeps the agent's original tool
inputs. With `task_overrides` in place, every user correction becomes a
labelled example.

ML pays off where the LLM is systematically wrong *for this user* —
durations, implicit deadlines, recurring noise senders. It doesn't pay
off where the decision needs reading comprehension (an explicit "by
Friday") or where data per class stays tiny.

| Decision | ML? | Model | Training signal | Use | Minimum data |
|---|---|---|---|---|---|
| Task vs not task | Yes, gate + hint | Logistic regression on the input embedding + source features (source, sender domain, list headers) | Strong: not_task input opened; task dismissed. Weak: decisions untouched for 7 days | p < 0.03 → auto not_task without an LLM call; otherwise p goes into the prompt | 300 inputs, 30 overrides |
| Label | Yes, hint | Recency-weighted kNN over task embeddings (pgvector) | Final label after overrides | "Similar tasks: Uni 8/10" in the prompt; the LLM decides (labels change, new topics appear) | 20 per label |
| Duration | Yes — strongest case | LLM estimate × learned correction (same model as step 8) + kNN residual | Check-in observations, calendar resizes, estimate edits | The solver uses the corrected value; the agent sees it as a hint | 10 observations per label |
| Deadline, explicit | No | — | — | Stays LLM extraction | — |
| Deadline, implicit | Yes, default | Median of (final due − received) over kNN neighbours, per label and source | Due-date overrides on tasks with `due_date_is_explicit = false` | Replaces the LLM's guess when ≥ 10 neighbours agree; otherwise a hint | 30 overrides |
| Work type / effort | Later | Same as label | Overrides | Hint | 20 per class |

Evaluation: offline replay over the last weeks. For each decision a
model ships only if it beats LLM-only on held-out recent data (accuracy
for task / label, MAE in log-minutes for duration, MAE in hours for
deadlines). Track the override rate per field as the live metric.

Placement: fitting in `app/services/learn/`, parameters in
`learned_models`, predictions read via storage and added to the prompt
context in `agent/input/runner.py` — no new agent tool. numpy is already
in the lockfile through Haystack; add it to `pyproject.toml` explicitly
since the solver features and the learners both need it.
