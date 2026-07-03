# Prompt: adapt the kotx API for the 007 runs/tasks consolidation

You are working in the **kotx** repository. The 007 personal task agent
(separate repo) is consolidating its "Runs" tab into its normal Tasks and
Inbox: kotx tasks of kind `implement` and `review` will become first-class
007 tasks, kotx state transitions will appear in the 007 inbox, and the
old runs-only UI goes away. This document specifies every kotx-side change
needed, plus the context you need to make good decisions. Do not change
workflow semantics (Branch 1/2/3) beyond what is listed here.

## How 007 will consume kotx after the consolidation (context)

- Every GitHub subject gets one canonical identity in 007:
  `github:<owner>/<repo>#<number>`. kotx transitions, GitHub notification
  emails, and manually created 007 tasks all fold onto that key.
- A 007 task is created when a kotx task becomes actionable:
  - `implement`: on `drafting → draft` (TASK.md ready to review/start).
  - `review`: on entering `awaiting_approval` (REVIEW.md ready).
  - `resolve_conflict`: **never** becomes a 007 task (fully automatic);
    007 ignores those transitions.
- A 007 task created from a kotx run is marked done when the review is
  sent (`awaiting_approval → awaiting_external` via approve/comment) or
  the PR is merged (`→ done`). The 007 "done" button on a kotx-linked task
  calls `POST /api/tasks/:id/discard`.
- A 007 task that *created* the GitHub issue (via `POST /api/runs` with
  `title`) adopts the kotx task that appears for it later — matched by
  `repo` + `subjectNumber`. No second 007 task is created.
- 007's LLM agent enriches the created task (estimation, due date) from
  TASK.md/REVIEW.md — kotx does not need to provide estimates.
- 007 suppresses its own Gmail-agent runs for `push`, `review_requested`,
  and `assign` notification emails on repos kotx tracks (they are linked
  to the task silently); `mention` and failed `ci_activity` emails are
  still processed because kotx does not cover them. For this, 007 needs
  to know the tracked repo list (see "New: repos endpoint").
- 007 will index `TASK.md`, `REVIEW.md`, and the proposed PR title/body
  into its search index ("documents" table), including for **terminal**
  tasks — those GET endpoints must keep serving content after done/
  cancelled, not 404.

## Required API changes

### 1. New: state-transition webhook (the core change)

007 must learn about transitions without tight polling.

- On every task state change, `POST` to a configured URL
  (`WEBHOOK_URL` — 007 serves this at `https://<007-host>/webhooks/kotx`):

  ```jsonc
  {
    "event": "task.state_changed",
    "previousState": "drafting",
    "task": { /* the full task object from GET /api/tasks/:id */ },
    "occurredAt": "2026-07-03T10:00:00.000Z"
  }
  ```

- Sign the raw body with HMAC-SHA256 using a shared secret
  (`WEBHOOK_SECRET`), sent as `X-Kotx-Signature: sha256=<hex>`. 007
  verifies before parsing (hard requirement on its side).
- Fire for all kinds (007 filters `resolve_conflict` itself). Include
  terminal transitions.
- Retry with backoff on non-2xx (a handful of attempts is enough); order
  does not need to be guaranteed — the payload carries full task state,
  so 007 treats each delivery as an upsert.

### 2. New: `updatedSince` polling fallback on `GET /api/tasks`

Webhooks can be missed (007 restarts). Add
`GET /api/tasks?updatedSince=<ISO timestamp>` returning every task —
**including terminal ones** — whose `updatedAt` is at or after the
timestamp, regardless of `scope`. 007 will call this on startup and as a
low-frequency reconciliation loop.

### 3. Changed: no new runs for follow-ups on a known subject

Today a repeated review request "creates or refreshes" a review task, and
follow-up activity can enqueue new work. New rule: while a **non-terminal**
task exists for `(repo, subjectNumber, kind)`, any trigger for the same
tuple must update/refresh that task (and fire the webhook) instead of
creating a new row. New runs for the same subject are only allowed after
the previous task is terminal. This keeps the 007 side at exactly one task
per issue/PR per kind.

### 4. New: repos endpoint

`GET /api/repos` → the enabled repo allowlist from `config/repos.yml`:

```jsonc
[
  {
    "fullName": "TUM-Social-AI/AflaConnect",
    "aliases": ["Social Ai"],
    "assignee": "your-login",
    "mergeMethod": "squash"
  }
]
```

007 uses this to (a) route issue creation without hardcoded aliases, and
(b) know which repos' GitHub emails to demote. Keep it cheap and static.

### 5. Guarantee: document endpoints stay readable after terminal states

`GET …/task`, `GET …/review`, `GET …/pr` must return their last content
for `done` / `failed` / `cancelled` tasks (007 indexes them for search).
If any of these currently 404 after termination, fix that.

### 6. Small contract confirmations (no change expected)

- `task.title` should be populated for all new tasks (007 uses it as the
  task title before the brief is enriched).
- `repo`, `subjectType`, `subjectNumber`, `kind`, `state`, `prNumber`,
  `trackedPrNumber`, `branch`, the `can*` flags, and `proposes` remain as
  documented — 007 renders its action button directly from `can*` +
  `proposes` and calls `start` / `approve` / `comment` / `merge` /
  `discard` accordingly.

## Endpoints 007 will stop using (drop if 007 is the only client)

- `GET /api/tasks/:id/prompt` — the consolidated modal has no prompt tab.
- `GET /api/tasks/:id/log` — no log tab either (and with it the whole log
  pagination machinery, if nothing else consumes it).
- `GET /api/containers` — the containers view is not carried over.
- `GET /api/tasks/:id/merge` (raw markdown) — 007 uses
  `GET …/merge/context` exclusively.

Everything else stays: tasks list/detail, `GET/PUT …/task`,
`GET/PUT …/review`, `GET/PUT …/pr`, `GET …/merge/context`,
`POST …/start | approve | comment | merge | discard`, `POST /api/runs`,
`GET /healthz`.

## Frontend anchors (mention in docs / any emitted links)

The 007 frontend is retiring the standalone runs modal. Deep links change:

- Old: `#run/<kotxTaskId>`
- New: `#task/<007TaskId>` — the consolidated task modal, which now shows
  the branch / PR / issue header, a single markdown tab (TASK.md,
  REVIEW.md, or PR.md depending on state), and the action buttons.
- 007 keeps `#run/<kotxTaskId>` as a redirecting alias that resolves to
  the adopting task, so existing links don't break. If kotx docs (SPEC.md,
  README) or any notification text reference the runs view, update them to
  the task anchor form.

## Acceptance checklist

- [ ] Webhook fires on every transition, HMAC-signed, full task payload.
- [ ] `GET /api/tasks?updatedSince=…` returns terminal + non-terminal.
- [ ] Repeat triggers on `(repo, subjectNumber, kind)` refresh the
      existing non-terminal task; no duplicate rows.
- [ ] `GET /api/repos` lists the enabled allowlist with aliases.
- [ ] `…/task`, `…/review`, `…/pr` readable on terminal tasks.
- [ ] Dropped endpoints removed (or explicitly kept with a reason).
- [ ] Docs updated for the new task anchors.
