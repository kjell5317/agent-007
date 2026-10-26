# Cleanup plan — after Jev takes over, and the legacy scheduler

Two independent cleanups. Part A removes LLM code that Jev triage makes
redundant once Jev has proven itself in production, then moves every
remaining LLM call to Gemini Flash and deletes the Anthropic path. Part B
deletes the rule-based scheduler the CP-SAT solver replaced. Both are mostly
deletion; the few behaviour changes are called out.

State on 2026-10-25:
- Jev is implemented but off (`JEV_MODE=off`, no key).
- The agents run on Anthropic (`CLAUDE_MODEL=claude-haiku-4-5` in prod);
  chat, the subtask planner and link research already run on
  `gemini-3.8-flash`.
- Prod runs `SCHEDULER_ENGINE=solver`, but the code default is still
  `shadow`, which keeps the legacy planner authoritative wherever the env
  var is missing (dev, tests, a fresh deploy).
- Prod has commute routing configured (home address, Maps key, calendar
  id).

Line numbers below are from that date.

---

## Part A — after Jev takes over

### Exit criteria (all must hold before any A-step)

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

### A1 — Widen Jev, then retire the regex cues it replaces

These are small behaviour changes that let the later deletions happen:

- **Manual event cue.** Ask `has_event` in the manual call and route on it,
  then delete the `mentions_event` regex check in `queue._process`. The
  regex itself in `services/event_context.py` stays only if another caller
  remains.
- **Promotions.** Also run the manual fast path for promoted Gmail/Slack
  inputs (`POST /tasks/open/{id}`) that have no candidates and no context
  inputs. `extract_task_fields` then only handles candidate updates, event
  cues and multi-message threads.
- **kotx briefs.** `agent/kotx/runner.py:297` runs `extract_task_fields`
  only for estimation and due date. Replace it with the Jev field call (no
  writer — the title comes from the GitHub subject). One LLM call fewer per
  kotx task.
- **Split cue.** Remove `_CONJUNCTION` from `services/task/split.py`.
  `needs_split` keeps the over-120-minute rule; a conjunction no longer
  matters without Jev's `split` answer.

### A2 — Delete what Jev made redundant

| What | Where | Why it can go |
|---|---|---|
| Flash batch path of the attribute classifier | `services/attribute_classifier.py`: `numbered`, `tool`, `parse`, `MAX_TOKENS`, the `chat` branch | A Jev outage leaves events/tasks pending; `classify_pending_events` and the backfill retry them next run. Also drop `tool_name`, `description`, `items` and `system_prompt` from `BatchClassifier` and its two definitions, plus the `CLASSIFIER.parse` / `.tool` tests |
| Cosine `auto_not_task` | `orchestrator.py`, `input_dedup_threshold` for the not-task side | **Decide on replay data first.** Compare Jev with the cosine gate on the `auto_not_task` rows. If Jev agrees ≥ 99 %, let Jev decide those too (the precedent is already in its state) and keep only `auto_duplicate`. That removes a second, uncalibrated drop path |
| `confidence` on the agent tools | `agent/tools/schemas.py` `_CONFIDENCE_SCHEMA`, its uses, `trace["confidence"]` writes | Jev's probability is the calibrated number; the LLM's self-reported confidence isn't used for anything. Keep `reason`: it is the NOT_TASK precedent text Jev reads |
| Classification guidance in the agent prompt | `agent/prompts/new_input.py` | The agent now only sees hard cases (events, uncertain inputs, edits). Shorten the "is it a task" section to what those need. Do this as a prompt edit plus replay, not a blind cut |
| ML proposal rows Jev replaced | `docs/scheduling-plan.md` "ML in the agent's decisions" | Mark the task-gate row implemented (the Jev triage model) and the label-kNN row superseded, both pointing to `docs/jev-plan.md` |
| Unused override readers | `db/clients/overrides.py:80` `list_for_field`, `:97` `task_ids_with_user_override` | No callers (verified). They were prep for the ML proposal that Jev replaced |

### A3 — Run every remaining LLM call on Gemini Flash

After A1–A2, the LLM does two kinds of work:

- **Writing text:** the triage writer, the notes harvest, note moderation
  and subtask plans.
- **Hard cases:** calendar events, edits to existing tasks, and inputs Jev
  isn't sure about.

Chat already runs on `gemini-3.8-flash` with tools, forced tool calls and
thought signatures, so the Google path through `agent/helpers/llm.py` is
proven. One provider removes the Anthropic SDK, its key, and the
two-provider branches in the adapter.

LLM call sites today:

| Call site | Provider in prod today |
|---|---|
| New-input agent (`agent/input/runner.py`), thread follow-up (`agent/thread/runner.py`), manual extractor (`agent/manual/runner.py`, also kotx briefs until A1) | default → Anthropic `claude-haiku-4-5` |
| Triage writer (`agent/triage/write.py`), notes harvest (`agent/triage/runner.py`) | default → Anthropic |
| Note moderation (`services/note/moderate.py`) | default → Anthropic |
| Subtask planner (`services/task/split.py`) | Flash (whenever `GEMINI_API_KEY` is set) |
| Attribute classifier fallback | Flash (deleted in A2) |
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
   - the same corrections as the exit criteria: dismissals, promotions,
     label and estimation overrides;
   - `no_tool_call` and `max_iterations` outcomes;
   - writer failures (`jev triage · writer failed` in the logs);
   - title quality, now written by Flash: spot-check 20.
4. **Delete Anthropic** once the gate holds:
   - **Adapter** (`agent/helpers/llm.py`): the `AnthropicChatGenerator`
     import, `_anthropic_generator`, `CACHE_CONTROL` and the cache marker in
     `_to_haystack_messages`, and the Anthropic branches of
     `_build_generator`, `_generation_kwargs` and `_tool_choice`. Gemini's
     own context caching covers the repeated system/tool prefix; no explicit
     marker is needed.
   - **Routing that becomes constant:**
     - `provider="google" if … else None` in `services/task/split.py` and
       `services/attribute_classifier.py`;
     - `provider="google"` in `agent/helpers/web.py`;
     - `Settings.llm_target` and the `chat_llm_provider` / `chat_llm_model`
       pair. Chat keeps `chat_thinking_level` if it should think
       differently from the background flows.
   - **Settings:** remove `anthropic_api_key`, `claude_model` and
     `DEFAULT_ANTHROPIC_MODEL`. `llm_provider` goes or becomes google-only.
     The `llm_model` default becomes `gemini-3.8-flash`.
   - **Dependencies:** `anthropic-haystack` and `anthropic` in
     `pyproject.toml`, plus the `MessageParam` pin comment; then
     `uv lock`.
   - **Config:** the LLM section of `.env.example`; `ANTHROPIC_API_KEY` and
     `CLAUDE_MODEL` in the prod env.
   - **Comments:** the `observability.py` module docstring (it names
     `AnthropicChatGenerator`), the settings comment about the "cached
     Anthropic path", and the `_generation_kwargs` docstring.

### A4 — Consider, not cleanup

- **Default `jev_mode` to `on`.** Keep the setting itself: `off` is the
  kill switch.

### Tests to update in Part A

- `test_attribute_classifier_jev.py`: the fallback test becomes "outage
  leaves the batch pending".
- The parse/tool tests in `test_calendar_event_attributes.py` and
  `test_backfill_task_attributes.py`.
- `test_task_split.py` conjunction tests.
- `test_manual_task_creation.py` event-cue tests.
- `test_kotx_consolidation.py` extract-fields stubs.
- The Anthropic cases in `test_llm_adapter.py` and `test_llm_tracing.py`
  (A3). Fake responses elsewhere that only label themselves
  `provider="anthropic"` can stay or be relabelled.

---

## Part B — legacy scheduler

The solver has been authoritative in prod. The legacy planner, the
legacy-only commute planner and the shadow plumbing are still in the code
and still run whenever `SCHEDULER_ENGINE` isn't set. Roughly 1,500 of
`schedule.py`'s 1,916 lines are legacy-only, plus most of
`commute/legs.py` (615) and about half of `commute/planner.py` (1,012).

### B0 — Make the solver the default (one-line changes, ship first)

- `config/settings.py`: `scheduler_engine` default `"shadow"` → `"solver"`.
- `services/plan/schedule.py:1899` `scheduler_engine()`: its getattr
  fallback is `"legacy"`, which contradicts the settings default. Make it
  `"solver"`.
- `.env.example`: `SCHEDULER_ENGINE=solver` and update the comment.

No prod behaviour change; it closes the "fresh deploy silently runs
legacy" hole.

### B1 — Port the two behaviours only the legacy tests cover

Before deleting legacy tests:

- **Repeat occurrences.** No solver test covers the earliest-start rule
  (`replan.py:490-495`). Port the intent of `test_recurrence.py:169,309`.
- **Located task inside a location blocker.** Done 2026-10-26. The solver
  lets a task at a show-as-free blocker's own place sit inside it, with no
  trip of its own (`solver._inside_blockers`). The new tests are in
  `test_scheduling_commute.py` and `test_scheduling_replan.py`, so
  `test_trip_block.py:2058,2095` can go with the legacy planner.

### B2 — Fix the one legacy call path that's still live under the solver

`api/notifications.py:208-213`: the notification's RESCHEDULE action calls
`schedule_task(..., block=scheduled_interval_for(task))` directly. It
bypasses `services/task/reschedule.reschedule_task`, which the UI button
uses: no "rescheduled" log row, no refit, no `pull_in` for deferred tasks.
Route it through `reschedule_task` and rewrite `test_scheduled_date.py:529`.

### B3 — Delete the legacy planner

`services/plan/schedule.py`:

- **Keep** (the solver path imports these):
  - `_schedule_lock`, `Interval` and the solver branch of `schedule_task`;
  - `schedule_tasks`, `announce_placement`, `drop_if_closed`,
    `reschedule_event`;
  - `_replan_window_for_commute`, `_task_for_event`;
  - `scheduled_interval_for`, `_duration_minutes`, `scheduler_engine`,
    `estimate_minutes`.

  Move them to a smaller module (e.g. `plan/placement.py`) or keep the
  file trimmed.
- **Delete:**
  - the legacy branch of `schedule_task` (213-250);
  - the constants at 67-90;
  - `BusyEvent` / `Gaps` / `PlannedSlot` (104-155);
  - `_schedule_task_locked` (336-476);
  - `_log_legacy_placement` (495-516);
  - `plan_task_slot` (542-782);
  - `_repeat_occurrence` (788-793, a duplicate of `replan.py:521`);
  - trip-block geometry, piggyback and chain insert (796-1112);
  - leg estimation helpers (1118-1239; the doc's "keep for the commute
    planner" note is stale — nothing imports them);
  - free-slot sweeps (1245-1336);
  - displacement repair (1342-1633);
  - conflict helpers (1636-1680);
  - the busy view (1686-1802);
  - `_replan_commutes_around` (1841-1872);
  - `_busy_calendar_ids` (1908-1916);
  - the module docstring parts about trip blocks and displacement.
- `services/plan/__init__.py:33-34,43,47`: drop the `PlannedSlot` /
  `plan_task_slot` re-exports.

Elsewhere:

- `services/plan/update.py`:
  - keep `_update_with_solver` and `_replan_window`;
  - delete the legacy branch (32-80), `PLAN_TRIGGER_FIELDS`,
    `_patch_requires_reschedule`, `_overlaps_other_event`,
    `_current_event` and `_busy_calendar_ids`.
- `services/task/update.py:19-29`: the legacy branch of
  `plan_trigger_fields` and its duplicate `PLAN_TRIGGER_FIELDS`.
- `cron.py`:
  - delete the legacy per-task retry (153-165); keep `NO_SLOT_RETRY`,
    whose solver key is "solver";
  - delete the shadow nightly run (290, 313-314);
  - fix the stale "displacement search" comment (57-60);
  - optionally rename `_overdue_scheduled_tasks` /
    `OVERDUE_TASK_INTERVAL_S` / job "overdue-scheduled-tasks" to what they
    do now (check-ins).
- `services/plan/replan.py`: delete `shadow_soon` / `_shadow_run`
  (183-214), the `dry_run` parameter, and `_hand_placed_locks` if only
  shadow uses it (229, 417).
- `services/notify.py:354` `notify_unroutable_location`: its only caller is
  legacy. The `notify_no_slot` docstring's "normal and extended windows"
  (317) is stale.
- `services/calendar/discover.py:708`: the engine check becomes
  unconditional.

### B4 — Delete the legacy commute planner

The solver no longer falls back to it (done 2026-10-26). When the routing
input fails, the solve runs without commute, the last good legs stay, and a
retry solve follows 10 minutes later (`replan._retry_routing_soon`). The
legacy commute planner is now only reachable through the legacy engine.
Delete it together with B3:

- `commute/reschedule.py` (whole file). Its promised "batch summary
  notification" never existed.
- `commute/planner.py`:
  - **delete:** `refresh_weather_sensitive_commutes`,
    `plan_window_commutes`, `_legs_to_write`, `_reschedule_candidates`,
    `_reschedule_task_anchors`, `_resolve_routes`,
    `_resolve_missing_transit`, `_home_rain`, `_resolve_routable_anchors`,
    `_partition`, `_read_calendar_ids`, `_context_margin`, `_PIN_LOOKBACK`;
  - **keep** (the solver's `routing.write_legs` and `flip.py` use them):
    `_write_legs`, `_immovable_conflicts`, `_notify_new_failures`,
    `_notify_new_conflicts`, `_sync_anchor_reminders` and its helpers,
    `_summary_for`, `_description_for`, `_navigation_url`, `_needs_patch`,
    `_commute_mode`, `delete_stray_commute_legs`.
- `commute/legs.py`:
  - **delete:** `choose_mode`, `required_routes`, the cluster helpers,
    `derive_legs`, the chain-harmonizing helpers, the leg builders,
    `Anchor` and `Durations`;
  - **keep:** `HOME`, `PlannedLeg`, `_norm`, and `FAILED_MODE` if
    `_write_legs` needs it.
- `plan/commute.py`:
  - **delete:** the legacy branches (75-93, 105-109);
  - **keep:** `commute_window_margin`, `_refresh_solver_weather`,
    `cleanup_stray_commute_legs`.
- `schedule.py:197-202`: the `_depth > 0` short-circuit, which only existed
  for nested calls from the legacy commute planner.
- **Settings:** `commute_home_layover_minutes` once
  `commute_window_margin` no longer reads it. Fix the "disabled by default"
  comment on `commute_enabled` (the value is True).

### B5 — Dead code (no callers; verified with grep over app and tests)

| Symbol | Where |
|---|---|
| `precipitation_probability_at`, `max_precipitation_probability_between` | `services/commute/weather.py:28,57` |
| `overdue_due_open` (+ `test_scheduled_date.py:311`) | `db/clients/tasks.py` |
| `ScheduleLogKind` (also lacks "rescheduled") | `db/models/scheduling.py:12-25` |
| `all_for_tasks` | `db/clients/schedule_log.py:108` |
| `LABELLED_KINDS` | `services/learn/jobs.py` |
| `"not_needed"` scheduling branch (`due_date` is NOT NULL since migration 0014) | `services/task/queue.py` |

### B6 — Tests

- **Delete with B3:** the 32 legacy-planner tests in `test_trip_block.py`;
  `test_scheduled_date.py:468,1051`; `test_recurrence.py:169,309` (after
  B1); `test_scheduling_replan.py:162` (`dry_run`).
- **Delete with B4:** all 35 tests in `test_commute_legs.py`, and the legacy
  commute tests in `test_trip_block.py` (570, 631, 816, 944, 1262, 1456,
  1471, part of 522).
- **Keep:** the shared-writer, discover and blocker-predicate tests in
  `test_trip_block.py` (69, 110, 661, 707, 762, 800, 869, 893, 979, 996,
  1015, 1027, 1047, 1084, 1195, 1225, 1345, 1395, 1439, 2013, 2141, 2163).
  Consider moving them to a file named after what they test.
- **Rewrite:** `test_scheduled_date.py:1032` patches `_schedule_task_locked`
  but tests an engine-agnostic rule.

### B7 — Open questions (decide before touching)

- **`tasks.scheduled_date` with `server_default now()`.** This is a
  legacy-era "provisional slot" that the solver ignores, and `checkin.py`
  works around it. `TaskRead.schedule_status` (`db/schemas/task.py:197-210`)
  and the frontend's `schedule_status` derive from it; the solver's
  `schedule_state` is typed in the frontend but unread. Consolidating on
  `schedule_state` is a schema + frontend change — worth it, but separate.
- **`discover.py` overlap path** (244-273): a hard `event_buffer_minutes`
  gap check that hands off to `reschedule_event`. Under the solver this
  could be one coalesced re-solve. Keep `event_buffer_minutes` either way —
  it is also the related-event deadline offset (`event_context.py`).
- **The `scheduled_overdue_reschedule` points action name** is legacy, but
  it is still written by check-ins and is a unique-index dedupe key. Leave
  it, or rename it with a migration.

### Order

**B0** → **B2** → **B1** (decision) → **B5** → **B3** + its tests → **B4**
+ its tests → **B7** as separate work.

B0, B2 and B5 are safe, independent PRs. B3 and B4 are large deletions:
one PR each, and each runs the full suite plus a day of prod solver logs
before merge.
