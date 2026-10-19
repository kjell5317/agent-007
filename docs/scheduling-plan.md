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
- **Phase 2** — commute inside the solver (the bike may stay anywhere;
  ride length and rain become learned preferences, trained by a flip
  link on every commute event), work type / effort for calendar events.

A separate section at the end proposes where ML can take over or assist
decisions in the extraction agent.

## Decisions and assumptions

| Topic | Decision |
|---|---|
| Time grid | 5 minutes. Durations and commute legs round **up** to 5 min. |
| Solver | OR-Tools CP-SAT (new dependency). 10 s limit per trigger, 60 s nightly, warm-started from the current calendar. Result = best schedule found within the limit, never worse than the current one; optimality is not proven at realistic sizes (see *Solver limits and horizon*). |
| Horizon | 7 days, the same look-ahead as the commute planner. Tasks due later may be *deferred*: not placed now, reconsidered on every solve (step 6). |
| Hard: overlap | No two tasks overlap; no task overlaps a calendar event. |
| Hard: events | Calendar events not owned by the app never move. |
| Hard | Nothing starts in the past. `depends_on_task_id` order holds. A repeat occurrence doesn't start before its period. A task whose slot has started stays fixed, including a running *Still working* extension. |
| Deadline | Per task: `hard`, `firm` or `soft` (step 3, step 5). Lateness is penalised by kind; an overdue task gets placed instead of failing. |
| User-placed tasks | Movable. A strong anchor while their surroundings are unchanged; normal stability once the surroundings change (step 6). |
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
| `booked_minutes` | length the current slot was booked with (the corrected estimate); null = the estimate. Calendar sync compares event lengths against it, so a corrected booking doesn't read as a user resize |
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

Backfill: a one-off script compares, for tasks due at most 7 days ago, each task's agent tool-call input in
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
- Backfill script for tasks due at most 7 days ago, one batched call
  per 20 tasks on the cheap model.

### Step 4 — End-of-slot check-in (replaces the penalty notification)

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

### Step 5 — Feature extraction (pure)

`app/services/plan/features.py`. Input: a `SlotContext` dataclass (task
attributes, slot start/end, previous and next neighbour with kind / label
/ work type / effort / gap, day load). Output: sparse
`dict[str, float]`. The same function produces log snapshots and solver
cost tables, so training and serving can't drift apart.

Utility units are log-odds; prior values below are the starting weights
that approximate today's rules.

| Feature | Encoding | Prior |
|---|---|---|
| Hour of day | one-hot, 24 | 10–19: 0 · 8–10 and 19–24: −2 · 0–8: −8 |
| Hour × work type, hour × effort | one-hot | 0 |
| Weekday | one-hot, 7; × work type | 0 |
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
group (step 7).

### Step 6 — Solver

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
    step 7). One linear constraint per week, almost free to solve.
  - Effect: a task due in two months with only poor slots this week
    waits; one whose remaining weeks are already full is pulled in now.
- `AddNoOverlap` over task intervals and fixed intervals (fixed tasks,
  calendar events). Gaps are soft (features), not padding.
- Costs are built from small tables, never one entry per start:
  `day_i = start_i // 288`, `tod_i = start_i % 288`, `hour_i = tod_i // 12`.
  Hour terms (hour, hour × work type, hour × effort) fold into a 24-entry
  table per task, day terms (weekday, season, days from now and
  their work-type interactions) into one entry per horizon day, lateness
  is linear via `AddMaxEquality`. A prototype with one 4032-entry table
  per task spent 25 s in presolve for 40 tasks; the compact form accepts
  the warm start in under 0.5 s.
- Gap and neighbour terms need one yes/no variable per (task, item it
  could sit next to) pair: "ends 0 / 5 / 10 min before", "starts 0 / 5 /
  10 min after", "adjacent within 60 min". Generated for every horizon day by
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
- Durations use the duration correction from step 7 once it exists.

Integration:

- `schedule_task(session, task, ...)` keeps its signature (≈20 call
  sites). Internally it marks the user's schedule dirty and runs
  `replan()` under the existing `_schedule_lock`; a burst of triggers
  coalesces into at most one follow-up solve.
- `replan()`: fetch busy events, build `SolveInput`, solve,
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
2 vCPUs. Hence the fallback threshold in step 6. "Global optimum" in practice means: the best schedule found in the
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

Horizon: 7 days, matching the commute planner. The tables above were
measured on 14 days, so a week is well inside the budget. The solver handled 180 days in tests, but far
placements get re-solved many times before they happen and each move is
a calendar write. Deferral covers later deadlines.

### Step 7 — Learning

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
  rare weekdays) vary more, so the solver tries them now and then.
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
  `a = 0, b = 1` with little data. The log-estimate is centred on one hour,
  so the shared slope doesn't absorb one group's offset.
- The solver uses the corrected duration; the UI shows both. An estimate
  the user set (UI edit or calendar resize) is booked as is.

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
4. Feature extraction, feature snapshots in the log.
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

Two steps: commute inside the solver, then work type / effort for
calendar events. Each runs in shadow mode against the phase-1 setup
before it becomes authoritative.

### Step 8 — Commute in the solver

Today the commute planner derives legs *after* placement, with hard
rules: a leg is ridden only under `commute_bike_max_minutes` (25) and
below `commute_rain_threshold_pct` (30 %), the bike must come back home
within the trip, and a gap of `in + 60 min + out` sends the user home.
Phase 1 moved commute out of the solver's view, so a located task can
land where its travel doesn't fit. Phase 2 puts travel into the model:
placements see their legs, the bike can be left anywhere and fetched
later, and the ride-length and rain rules become a learned preference
with today's thresholds as the prior. Every commute event carries a
link that flips its mode; the flips are the training signal.

#### Rules

| Rule today | Phase 2 | Why |
|---|---|---|
| The bike lives at home and returns within the trip | **Hard: the bike is ridden only from where it is.** It may stay anywhere, overnight too, and is ridden back when the route passes there again. Leaving it away overnight costs −0.3 per night (fixed, not learned) so it isn't parked out for nothing | Physical — but riding to the office and taking the train home in the rain is a valid plan |
| Mixed trips harmonised (all transit, else all bike) | Dropped: each leg picks its mode, subject to where the bike is | — |
| Bike only if ride ≤ 25 min | **Soft, learned.** Ride-length buckets with priors built around `commute_bike_max_minutes` | A 28-min ride that saves 25 min of transit is worth it |
| Bike only if rain < 30 % | **Soft, learned.** Rain-probability buckets with priors built around `commute_rain_threshold_pct`, plus rain amount | A 40 % shower risk on a 10-min ride beats a 45-min transit trip |
| Go home if the gap fits `in + 60 + out` | **Soft.** Away-time cost against the cost of an extra trip home; priors set so the break-even is today's 60 min at home | A long gap at the library may be fine |
| 5-min buffer between a leg and its anchor | Hard, part of the leg | Locking the bike, getting off the train |
| Legs dodge online meetings (`MAX_DODGE`) | Hard: legs never overlap any other item (task, online or location-less event) | The solver places the leg in a gap instead |
| Leg written over a fixed event when nothing can move | Soft: a leg between two fixed items may be shorter than its travel time at −0.5 / min; written at full length and notified as today | Fixed events can genuinely be too close |
| No route → 30-min placeholder | Same, with a −3 cost so placements avoid it | — |
| Piggyback / chain-insert placement | Emerges from travel cost and neighbour features | — |
| Bike and transit | Unchanged. On short routes Google's transit result is a walk, so walking needs no mode of its own | — |

#### Model

- **Locations:** home plus the distinct routable locations of located
  tasks and located events in the horizon (typically ≤ 10). Online and
  location-less items are "anywhere": no route node, but they block legs.
  Tasks that must happen at home get `location = home` (agent prompt
  tweak for chores).
- **Travel matrix:** bike times (time-invariant, cached forever) and
  transit times per pair, rounded up to 5 min. The model uses the
  conservative maximum of the daytime transit buckets; the written leg
  uses the exact departure-time lookup. Only pairs that can follow each
  other within a day are fetched. Soft rules need both modes for most
  pairs, where today transit is only fetched once the bike is ruled
  out: about `L · (L − 1)` pairs × (1 bike + 3 transit buckets), ~360
  Distance Matrix elements for 10 locations, then only new locations.
- **One ordered path per day.** The bike's location is a state that
  carries from trip to trip and across nights, so the day's movements
  must be ordered — independent trips can't express "the bike is still
  at the office from yesterday". Per day, `AddCircuit` over: a start
  node and an end node (home), located fixed events (overlapping ones
  merged into one node, as today), located movable tasks on their
  candidate days (skipped via self-loop when the task is on another
  day), home-located tasks, and `K` optional "home stop" nodes (default
  2) for going home between items. The arc end → start closes the path.
- **Bike state:** an integer `bike_at_i` per node — the location index
  of the bike while the user is at node `i`. On the chosen arc `j → i`:
  a ride requires `bike_at_j = loc_j` and sets `bike_at_i = loc_i`;
  transit keeps `bike_at_i = bike_at_j`. The end node of day `d` feeds
  the start node of day `d + 1`; the first start node takes the bike's
  current location (see *Bike location*). Arcs between nodes at the same
  place carry no leg.
- **Legs:** one arrival leg per present node, a variable-size interval
  in the global `AddNoOverlap` with tasks and events. With `ride_i` the
  mode of that leg and `arc_ji ∧ ride_i` literals, the duration stays
  linear: `dur_i = Σ_j arc_ji · transit_ji + (arc_ji ∧ ride_i) · (bike_ji − transit_ji)`.
  `arc_ji ⇒ leg_i.start ≥ leave_j`, `leg_i.end + buffer ≤ start_i`.
- **Away time:** per day, arrival at the end node minus departure from
  the start node minus time at home stops — linear.
- **Rain by hour:** per node, an element on the leg's hour of the horizon
  (7 × 24 = 168 entries) gated by `ride_i`. Small tables only, as in
  phase 1.
- **Weather input:** Open-Meteo hourly precipitation probability, amount,
  temperature and wind at home, one call per solve (cached 1 h).
  Probabilities more than 24 h out are blended toward the local base rate
  (weight 1 at 24 h, 0.3 at 7 days) so a noisy forecast six days out
  doesn't decide today's placement; the hourly weather refresh re-solves
  when a probability for the next 48 h moves by ≥ 10 points.
- **Pinned modes:** a flipped leg's mode is a strong preference (−20 if
  the solver uses another mode on that leg), not a hard constraint — a
  flip to the bike when the bike can't get there stays satisfiable.

#### Bike location

The model needs to know where the bike is now. It is derived, not
entered: the last leg that has already started and was ridden (from the
commute events, which carry their mode) leaves the bike at its
destination; with none in the last 14 days, the bike is at home. Past
legs therefore stay on the calendar for 14 days instead of being deleted
when they end. Flipping
a past leg corrects it (see below). Leg descriptions name the bike's
location whenever it isn't at home ("Bike is at Uni since Tue — riding
it back"), so a wrong belief is visible before it matters.

#### Flip link

Every commute event's description gets one line, e.g. "Switch to
transit → <link>" (or "to bike").

- **Link:** a signed URL (HMAC over leg event id, target mode and
  expiry, with the notification action secret), valid until 24 h after
  the leg ends. Exempt from the login check, like
  `/notifications/actions`.
- **Confirm page:** the link opens a one-button page ("Bike → Transit
  for Uni 9:40?") that POSTs; a plain GET never changes anything, so
  link previews and prefetchers can't flip a leg.
- **Future leg:** pins the leg's mode, logs the flip, re-solves. The leg
  is rewritten with its new mode and duration, and whatever the change
  implies for the bike (it stays at Uni, so tomorrow's first leg goes by
  transit) follows from the solve.
- **Past leg (within 24 h):** records what actually happened: the leg's
  event is rewritten, the bike's derived location changes, the flip is
  logged, and the future is re-solved from the corrected bike location.
- The page answers with the outcome, e.g. "The bike is at Home — this
  leg stays on transit" when a pin to the bike can't be met.

#### Learning from flips

Same approach as slots: the commute terms are a new feature group in the
preference model (`commute_*`), so the posterior, the daily Thompson
draw and the refit code are shared. A flip is a pairwise row,
`x = φ(leg, mode taken) − φ(leg, mode planned)`, `y = 1`, with the usual
time decay. Legs nobody flips are not counted as confirmations — that
would only reinforce whatever was planned; exploration comes from the
daily posterior draw instead, which now and then rides in light rain or
a little over the threshold.

New table `commute_log`: leg key, date, origin, destination, planned
mode, flipped-to mode, the features of both modes, whether the leg was
past, created_at.

#### Features and priors (log-odds)

The thresholds become the prior's reference points: the ride-length
bucket boundaries are 0.6×, 1×, 1.4× and 1.8× `commute_bike_max_minutes`
(15 / 25 / 35 / 45 min by default), the rain buckets put their big step
at `commute_rain_threshold_pct`.

| Feature | Encoding | Prior |
|---|---|---|
| Travel time | per minute, by mode | bike −0.02 · transit −0.02 |
| Transit leg | flag per transit leg (waiting, transfers, delays) | −0.3 |
| Ride length | bucket per bike leg: ≤ 15, 15–25, 25–35, 35–45, > 45 min | 0, 0, −1, −2.5, −5 |
| Rain probability | bucket per bike leg: < 10, 10–20, 20–30, 30–50, ≥ 50 % | 0, −0.1, −0.4, −1, −2.5 |
| Rain amount | per bike leg, per mm/h | −0.5 |
| Cold, wind, dark | per bike leg: °C below 5, km/h above 25, after sunset | 0 |
| After a physical or high-effort task | per bike leg | 0 |
| Away from home | per minute between leaving and returning | −0.01 (not learned) |
| Extra trip home | per home stop used | −0.3 (not learned) |
| Bike away overnight | per night | −0.3 (not learned) |
| Leg shorter than travel | per minute (only forced between fixed items) | −0.5 (not learned) |
| Unroutable leg | per leg | −3 (not learned) |
| Mode change close to departure | per leg, within 2 h of departure | −2 (stability, not learned) |
| Pinned mode not used | per pinned leg | −20 (not learned) |

Checks against today's behaviour: a 30-min ride against 40 min of
transit goes by transit (−1.6 vs −1.1), but a 28-min ride against 70 min
of transit rides (−1.56 vs −1.7). 30 % rain on a 15-min ride against
25 min of transit goes by transit (−1.3 vs −0.8); against 60 min of
transit it rides (−1.3 vs −1.5). A gap between two
places 15 min from home sends the user home once about 60 min at home
fit — the current layover rule.

#### Solver limits (spike)

Synthetic week, 2 workers, 10 s, 8 locations, roaming bike state, `K = 2`
home stops per day, routing plus hour costs (no phase-1 neighbour
terms), warm start from a greedy schedule completed by a 2-s fixed-start
solve:

| Located tasks | Other tasks | Events / day | Located-task days | Variables | First solution |
|---|---|---|---|---|---|
| 3 | 20 | 4 | all 7 | 2.6 k | 1.1 s |
| 6 | 30 | 5 | all 7 | 4.8 k | 2.3 s |
| 10 | 30 | 5 | all 7 | 7.7 k | none in 10 s (hint not completed) |
| 10 | 30 | 5 | 3 candidates | 4.0 k | 1.7 s |
| 15 | 20 | 5 | 4 candidates | 6.7 k | 2.8 s |

The roaming bike costs about 1.5× the variables of a bike-returns-home
model. Consequences:

- Located tasks get route nodes on 3 candidate days (current day plus the
  best ones) from the start; all 7 days don't fit the budget beyond a
  handful of located tasks.
- The warm start is built directly, not by a probe solve: replay the
  current calendar (existing legs and their modes) and fill in new
  located tasks with a greedy route simulation that tracks the bike —
  every arc, mode, home stop and `bike_at` value set explicitly.
- With the phase-1 neighbour terms on top, the 10 s budget is tight
  above ~10 located tasks per week; the nightly 60 s run closes the gap.

#### Materialisation

The solution fixes, per day, the visiting order, home stops, each leg's
mode and times. Legs are written through the existing writer
(`_write_legs` diff by leg key, reminders via `_sync_anchor_reminders`,
conflict and no-route notifications as today). Chosen transit legs are
then looked up at their exact departure time; a leg longer than modelled
(rare, the model is conservative) shifts within its slack or triggers a
re-solve. The description names the deciding terms ("Transit — rain
40 % at 17:00"), the bike's location when it's away, and the flip link.
`derive_legs` stays for shadow comparison and is then deleted with the
trip-block code. Neither planner writes legs beyond the 7-day horizon.

#### Tests

Small instances (one worker): the bike can be left at a location and is
only ridden from there; leaving it overnight happens only when it pays;
rain and ride length are costs, not bans; a pinned mode is used when
reachable and reported when not; a flip of a past leg moves the derived
bike location; a home task never sits inside an away stretch; legs never
overlap online meetings; a shortened leg only between fixed items; the
layover break-even on today's fixtures. Regression: on days with only
fixed events and dry weather, the solver's legs match `derive_legs` on
the existing `test_commute_legs.py` fixtures.

#### Rollout

1. Flip links on today's legs plus `commute_log` — mode data starts
   accruing before the solver changes. Under today's planner a flip pins
   the whole trip's mode (it can't leave the bike yet).
2. Travel matrix and weather fields; solver with commute in shadow for a
   week, comparing legs and placements with the current planner.
3. Switch; delete the trip-block planner code and the
   `reschedule_overlapping_tasks` path.
4. Commute features join the learning refit once flips exist.

#### Risks

- **Bike-location drift.** If the bike moves outside the plan (someone
  else fetched it), the model's belief is wrong until a past leg is
  flipped. Leg descriptions show the believed location to make this
  visible.
- **Solve time** with routing and neighbour terms together: candidate
  days, a direct warm start, the nightly long run.
- **Distance Matrix cost** for transit pairs: bounded by pair pruning
  and caching.
- **Forecast churn:** blending far-out probabilities, re-solving only on
  material changes, the stability cost near departure.
- **Few flips, slow learning:** the priors keep today's behaviour until
  flips accrue; the daily draw explores around the thresholds.

### Step 9 — Work type and effort for calendar events

Today events contribute only their label to neighbour features. Meeting
recovery and batching need their work type and effort too.

- **Rules first:** video link or other attendees → `social`; the
  label's defaults; all-day and show-as-free events get none.
- **Cheap model for the rest:** when the event cache ingests a changed
  event, classify the ones the rules leave open in batches with the same
  cheap-model call as the task attribute backfill. Store the values in
  `documents.metadata`.
- **Features:** event neighbours get the work type × effort pair
  features tasks already have; meetings count toward a new "minutes in
  meetings today" day-load feature (prior 0).
- No editing UI: a wrong value is corrected through the label defaults.

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
| Duration | Yes — strongest case | LLM estimate × learned correction (same model as step 7) + kNN residual | Check-in observations, calendar resizes, estimate edits | The solver uses the corrected value; the agent sees it as a hint | 10 observations per label |
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
