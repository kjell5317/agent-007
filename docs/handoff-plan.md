# Agent hand-off and email drafts — implementation plan

When the extraction agent creates a task, it also says in the same tool
call whether an agent could do the task or prepare it, or answers it
outright. Right after the task is saved, 007 runs that agent. Research and
email drafts start without asking; a code change waits for one tap. An
agent that completes a task closes it and sends an "Answered" push. Reply
drafts always come from the Gmail account's primary address.

Nothing here depends on Jev, which stays parked (`docs/jev-plan.md`).

**Status (2026-11-16):** implemented. Open:

- the before/after run of ~20 recent inputs from H1;
- `create_event` for the manual extractor.

Deploying needs migrations 0046–0048 and one Google re-consent for
`gmail.compose`; until then drafts fail with a "reconnect Google" error.

## Decisions

- **Same turn, same model.** `create_task` gets optional `handoff` and
  `answer` fields, so the agent that creates the task (Haiku in prod)
  decides in the same call. No extra classifier call.
- **Research and drafts run without asking.** At about 2.6 new tasks a day
  (30 days to 2026-11-15: manual 48, Gmail 21, Slack 8) they cost very
  little and only add text to the task or put a draft in a mailbox. No
  confidence score, no thresholds, no daily cap.
- **One research kind.** Searching the user's data and the web is one
  agent with all read-only tools, so the creating agent picks between
  three kinds, not four.
- **An agent may finish a task.** `answer` at creation (the notes already
  hold the answer) and research that reports `completes_task` both close
  the task and send "Answered: …" with the answer and a Reopen button. No
  completion points: the user didn't do the work.
- **A code change always needs a tap.** It creates a GitHub issue and
  starts kotx, in the repo the task's label is connected to (label
  settings, `labels.github_repo`).
- **No executor ever sends, merges, pays or books.** kotx keeps its own
  start/approve/merge gates. Email drafts are never sent by 007: there is
  no send code path.
- **Calendar entries aren't a hand-off.** The new-input agent
  (Gmail/Slack) already has `create_event` and creates events in the same
  run. The manual extractor only has `update_event`; if typed entries like
  "put the exam dates in my calendar" should create events, give it
  `create_event` as a separate small change.

## Hand-off kinds

| Kind | Typical tasks | Executor | Starts | Result |
| --- | --- | --- | --- | --- |
| `research` | "Find the landlord's invoice number", "Check visa rules for Japan", "Register for the workshop" (steps on the linked page), "Prepare slides for Friday" | `agent/handoff/research.py`: chat's read-only tools (tasks, messages, notes, calendar, contacts, Drive, Notion, GitHub, web search) plus `read_link` | right away | "Research" section in the description; due date / location corrected when a source states them; task closed when the findings complete it |
| `email_reply` | "Answer Anna about the meeting slot", direct questions by email | email drafts (below) | right away | draft in the Gmail thread; task closes when sent |
| `code_change` | "Fix the export bug", GitHub mention emails asking for a change | `services/kotx/runs.create_issue_run` with the label's repo | after a tap | GitHub issue + kotx run; the existing kotx flow takes over |

Not planned:

- **Calendar entries:** see Decisions.
- **Payments, bookings, forms:** financial or personal-data actions, and
  no tools for them.
- **Notion / Drive writing:** those grants are read-only today.
- **Slack replies:** the user scopes are read-only, and Slack's Web API
  has no documented drafts endpoint. Sending as the user (`chat:write`)
  would break the no-send rule. If wanted later: generate the reply and
  show it with a copy button and a link to the thread.
- **Home Assistant actions:** physical side effects.

## Step H1 — The agent decides at creation

`create_task` (`agent/tools/schemas.py`) gets two optional properties,
offered for Gmail, Slack and manual inputs:

```json
"answer": {"type": "string", "description": "Only when the input asks for information that what you already found fully provides: the answer, with its source."},
"handoff": {
  "type": "object",
  "properties": {
    "kind": {"enum": ["research", "email_reply", "code_change"]},
    "brief": {"type": "string", "description": "What the agent should do or find out, one or two sentences."}
  },
  "required": ["kind", "brief"]
}
```

- **Which agents:** the new-input agent (`agent/input/runner.py`, Gmail
  and Slack) and the manual extractor (`agent/manual/runner.py`, fresh
  entries and promotions). Both build their tools from
  `new_input_tools`. Thread follow-ups, kotx tasks, subtasks and
  recurrence instances get neither field.
- **Kinds per input (pure)**, `services/handoff/eligible.py`:
  `handoff_kinds(raw, repo_labels)` sets the enum.
  - `research`: always.
  - `email_reply`: Gmail source, and the sender is not a no-reply address.
  - `code_change`: when at least one label is connected to a repo; the
    field's description lists those labels.
- **Check after the call (pure):** drop `code_change` when the chosen
  label has no repo or the task already has a GitHub link.
- **`answer`:** the answer goes into the description under "Answer", the
  task is saved closed (no split, no scheduling) and "Answered: …" is
  pushed. A `handoff` next to an answer is ignored.
- **Prompt:** a short section in `agent/prompts/new_input.py` and
  `agent/prompts/manual.py`. The field descriptions say what each executor
  can do; the agent only sees text, so they are the contract.
- **Trace:** the choice goes into `trace["handoff"]` / `trace["answer"]`.
- **Check before shipping:** the prompt and tool change touch the
  task-creation agent. Run about 20 recent inputs locally before and
  after the change; the create / not-task / duplicate decisions must not
  change.

## Step H2 — Data model

Migrations `0046_task_handoffs` and `0048_handoff_research`:

| Column | Notes |
| --- | --- |
| `id` | uuid pk |
| `task_id` | fk → tasks, cascade |
| `kind` | one of the kinds above |
| `brief` | from the agent, or typed when started by hand |
| `state` | `proposed` \| `running` \| `ready` \| `failed` \| `declined` |
| `result` | jsonb: `summary`, `completed`, `changed`, `issue_url`, `draft_id`, `mail_url`, … |
| `trace` | jsonb: the agent's tool calls, shaped like a raw input's `agent_trace` |
| `created_at`, `updated_at` | |

At most one non-terminal row per task. `TaskRead.handoffs` lists them
newest first.

## Step H3 — Run

A task handed to an agent is never split automatically: the creation paths
skip the split when the agent set `handoff`, and `maybe_split_task` returns
nothing for a task with a hand-off row (later estimate edits). Splitting would
make it a parent task, where the result isn't shown. The user's explicit
"Split" still works.

`services/handoff/run.py`:

- `start(task_id)`: called right after the task is committed, as a
  background task with its own session, so task creation never waits.
  - `code_change` → `proposed` and a push with Open issue / Decline.
  - The other kinds → `running`, then the executor.
- A dict from kind to async function, with no base class. Each executor
  returns `(state, result, trace)`. A failure sets `failed`, calls
  `notify_error`, and leaves the task untouched.
- **`research`:** `run_research` (Gemini Flash, chat's tools plus
  `read_link`, the input's filtered links in the prompt) ends with a
  forced `report {findings, completes_task, due_date?, location?}`. The
  findings are appended under "Research"; a stated due date or location
  is applied. With `completes_task` the task is closed (no points) and
  "Answered: …" is pushed.
- **`code_change`** (on accept): `create_issue_run(task, repo=…)` with the
  label's repo sends `task.description` as the issue body, or the brief
  plus the cleaned input text when it's empty. `issue_url` goes on
  `task.link`, so the existing adoption path links the kotx task.
- **`email_reply`:** see "Email drafts" below.

## Step H4 — Surface

- **Notifications after a hand-off:**

  | When | Push | Buttons |
  | --- | --- | --- |
  | code change proposed | "Open issue? …" | Open issue / Decline |
  | research completes the task, or `answer` at creation | "Answered: …" with the answer (replaces the task's push) | Reopen |
  | research moves the due date | "Research moved the deadline: …" | — |
  | draft created or regenerated | "Draft ready: …" (opens the thread) | Regenerate / Discard |
  | new reply on a drafted thread | "New reply in the thread: …" | Regenerate / Discard |
  | an executor fails | the error push | — |

  Research that only adds findings is silent; the task's "Scheduled" push
  already went out. Sending the draft closes the task and clears its push.
- **API**, thin, in `app/api/tasks.py`, calling `app/services/handoff/`:
  - `POST /tasks/{id}/handoff`: accept a proposal, or start a kind by
    hand with an optional `brief`;
  - `POST /tasks/{id}/handoff/decline`;
  - `POST /tasks/{id}/draft/regenerate` and `POST /tasks/{id}/draft/discard`.
- **Frontend:**
  - a chip on `TaskCard` with the state ("Researched", "Answered", "Draft
    ready", "Agent can open an issue");
  - a panel in `TaskDetailModal` with the brief, the result, and
    accept / decline for proposals;
  - a "Hand off…" menu on any task; it offers a GitHub issue only for
    labels connected to a repo;
  - every traced run (research, drafts) is listed under "Linked inputs"
    with its trace, like an input.

Research output is ordinary description text; if it's wrong, the user
edits it like any description. No separate discard.

---

## Web research — replaces today's link research completely

### What exists today

- **Inline link research in the agents.** The new-input agent
  (`agent/input/runner.py`) and the manual extractor
  (`agent/manual/runner.py`) can get an optional tool,
  `research_input_link()` (`INPUT_LINK_RESEARCH_TOOL`, no arguments):
  - it is offered only when `TASK_WEB_SEARCH` is on, Gemini is
    configured, and the input contains a URL;
  - the agent decides whether to call it (at most once), but not which
    URL. `first_input_url` takes the first `https?://` regex match in the
    content, else the first entry of `source_metadata["urls"]`;
  - after the result comes back, the runner makes the agent decide again;
  - `research_link` in `agent/helpers/web.py` makes a separate Flash call
    with Google Search grounding ("Inspect this URL"). Grounding searches
    Google; it does not load the page, so pages that Google hasn't indexed
    or that need a login return nothing;
  - fresh manual entries pass `research=False`, so only promotions use it
    there. The thread follow-up agent doesn't have it.
- **In prod (checked 2026-11-15)** `task_web_search` is off. It was on
  from about 2026-09-28 to 2026-10-25. In that window the agent called the
  tool 20 times: 3 calls returned a usable summary, and none of the 20
  inputs became a task (all `not_task` or `duplicate`). The failures:
  - private pages: Notion, Microsoft Forms, LinkedIn;
  - tracking and redirect links: a PayPal footer link, `c.gle`;
  - a regex bug that captured a Markdown link (`url](url`) as one URL.
- **Leftovers of the old background research.** The `web_research` source
  was a follow-up input that patched the task. Its creation was removed on
  2026-10-01 (6df9545), but the handling is still there:
  - the `WEB_RESEARCH_SYSTEM_PROMPT` branch in `agent/thread/runner.py`;
  - special cases in `services/source_url.py`, `db/clients/tasks.py:37`
    (`is_manual_for`), `db/clients/raw_inputs.py` (`count_since`) and
    `events/publish.py:53`;
  - the "Web" labels in `frontend/src/lib/inbox.ts:64`,
    `components/notes/NoteCard.tsx:232` and the `App.tsx:293` filter.

  Prod has 2 such rows.
- **Chat's `web_search` tool** (`research_web`, `CHAT_WEB_SEARCH`) answers
  chat questions. It isn't task research and **stays**, sharing the
  research helper below.

### The replacement

All task research is the `research` hand-off (Step H3), run after the task
exists. Its agent reads links itself with `read_link`, which calls the
merged helper `research(url=…)` in `agent/helpers/web.py`: Gemini with
`url_context` (loads the page) plus Google Search. `task_urls(raw)` (pure)
gives it the input's links without tracking, unsubscribe, help-centre,
redirect and Notion/Drive/GitHub links (those go through their own
tools). Chat's `web_search` uses the same helper in query mode.

### Trade-off

The page no longer informs the "is this a task" decision. The prod window
above puts that cost close to zero: research ran almost only on inputs
that weren't tasks and never turned one into a task.

### Steps

- **W1:** the URL check, the merged helper with `url_context`, and chat's
  `web_search` on the new helper. No behaviour change for chat.
- **W2:** the executor, started from Step H3.
- **W3: delete the inline tool.** It is already off in prod, so no
  comparison week is needed.
  - `INPUT_LINK_RESEARCH_TOOL` (`agent/tools/schemas.py`, `__init__`);
  - the `research_enabled` / `researched_link` branches and the
    "decide again after research" loop in `agent/input/runner.py` and
    `agent/manual/runner.py`;
  - the `research` parameter of `extract_task_fields` and the queue's
    `research=False`;
  - `trace["web_search"]`;
  - the `research_input_link` sections in `agent/prompts/new_input.py` and
    `agent/prompts/manual.py`;
  - the `task_web_search` setting and `TASK_WEB_SEARCH` in `.env.example`;
  - `first_input_url` and `provisional_task_title` in
    `agent/helpers/web.py` if no caller remains;
  - the research tests in `test_manual_runner.py` and the agent tests that
    stub `research_link`.
- **W4: delete the `web_research` leftovers.**
  - the thread-runner branch (the research agent has its own prompt);
  - the backend special cases listed above;
  - the frontend labels and filter.

  The 2 historical rows then show the generic source label.

---

## Email drafts

Drafts always come from the Gmail account's primary address, whichever of
the user's forwarded addresses the mail was sent to (decided 2026-11-16).
No per-address routing, no Microsoft or IMAP mailboxes.

### Step E1 — Threading headers (pure)

`services/input/gmail/preprocess.py` also stores `references` in
`source_metadata`; `Message-ID` is already stored as `message_id_header`.
The reply's `In-Reply-To` / `References` are built from them.

### Step E2 — Where the draft is created

`users.drafts.create` in the original Gmail thread (`threadId`), `From` =
the primary "Send mail as" identity with its display name and signature
(`sendAs.list`). Needs the `gmail.compose` scope.

Code:

- `app/services/input/gmail/client.py` gains `create_draft` /
  `update_draft` / `delete_draft` / `get_thread` / `list_send_as`, but no
  send method.
- `app/services/email/compose.py` (pure) picks the primary sender and
  builds the RFC 2822 reply: `Re:` subject, threading headers, signature,
  quoted original with an "On …, … wrote:" line.

### Step E3 — Draft generation

`app/agent/draft/runner.py` + `app/agent/prompts/draft.py`: a lookup loop
over chat's read-only search tools (tasks, messages, notes, calendar,
contacts, Drive, Notion, GitHub, web search — the same set as
`own_data_lookup`), then a forced `write_reply {body, open_questions: [str]}`.

Context:

- **The thread:** `get_thread`, each message through the existing
  preprocess cleaning; earlier drafts in the thread are skipped.
- **The task:** title and description, plus the hand-off `brief` (what
  the reply must answer).
- **Free slots** when the mail asks for a time: a calendar search over
  the next two weeks.
- **Style examples:** 3 recent messages the user sent to the same
  recipient, else the same domain, else any recent sent mail.
- **Language:** the incoming mail's language.

Rules:

- Facts the agent doesn't have become `[[placeholders]]` and are listed
  in `open_questions`. Those are shown in the notification and the task panel.
- **Recipients are deterministic** (pure, in `compose.py`): `Reply-To`,
  else `From`. They are never taken from the LLM or from text in the
  body, so an injected "cc this address" can't add recipients.
- The prompt treats the thread as untrusted content.
- Model: Gemini Flash (`llm_target("chat")`), like the lookup: the draft
  agent uses chat's tools, so it runs on chat's model.

### Step E4 — Push and lifecycle

Migration `0047_email_drafts`:

| Column | Notes |
| --- | --- |
| `id`, `task_id`, `raw_input_id` | |
| `from_address`, `to`, `subject`, `body` | |
| `open_questions` | json |
| `provider_draft_id`, `provider_thread_ref` | Gmail draft id + thread id |
| `state` | `generated` \| `pushed` \| `stale` \| `sent` \| `discarded` |
| `created_at`, `updated_at` | |

- **Push:** the "Draft ready" notification and the task panel link to the
  Gmail thread.
- **New message on the thread:** the thread shortcut marks the draft
  `stale` and notifies "New reply in the thread" with
  `ACTION_DRAFT_REGENERATE`. Regenerating updates the draft in place
  (`drafts.update`); a second draft hand-off on the task does the same.
- **Sent detection:** the Gmail poll records the threads of `SENT`
  messages before skipping them. A live draft on one of those threads is
  marked `sent`, its task is closed, and a draft the user didn't use is
  deleted.
- **Discard:** `ACTION_DRAFT_DISCARD` deletes the Gmail draft, sets
  `discarded`, and sets the hand-off to `declined`.

### Step E5 — Triggers

- **The agent picks `email_reply`:** the draft is generated and pushed
  right after the task is saved, with a "Draft ready" notification:
  Regenerate / Discard (tapping the notification opens the thread).
- **Manually:** "Hand off… → Draft a reply" on any Gmail-sourced task.

### Security

- **Gmail scope:** adds `gmail.compose`. It also permits sending, so
  "no send" is enforced in code: the client has no send method, and a test
  asserts that no request goes to a `…/send` endpoint. Users re-consent
  once (`prompt=consent` is already passed).
- **Review:** every draft is reviewed by the user before sending.
  Recipients are deterministic.

## Rollout order

1. H1 and H2. Until the executors exist, the agent's picks are only
   stored and shown as a chip — a free week to see what it picks and
   tune the prompt section.
2. H3, H4 with `web_research` (W1–W4: the inline link research goes
   away), then `own_data_lookup`, then `code_change`.
3. Email: E1 → E2 → E3 → E4 → E5.

## Risks

- **The task-creation prompt changes.** The hand-off section and field
  could shift create / not-task decisions. The before/after run in H1
  checks this.
- **The agent judges text, not capability.** It can pick a kind an
  executor can't deliver. Mitigations: the prompt says what each executor
  can do, the eligibility rules, and a wrong pick only costs a few cents.
- **Prompt injection.** The agent and the executors read untrusted mail
  and web pages. Research patches are limited to three fields, draft
  recipients are deterministic, nothing is sent, and a code change needs
  a tap.
- **Research after creation:** a dropped input is never researched. The
  prod window shows this cost nothing so far (0 of 20 researched inputs
  became tasks).
- **Draft voice:** style examples from the user's own sent mail, plus
  open questions instead of invented facts.
- **`gmail.compose` is broader than drafts-only.** Gmail has no
  drafts-only scope; mitigated by the absent send path and the test.
