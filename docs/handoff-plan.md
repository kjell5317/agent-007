# Agent hand-off and email drafts — implementation plan

When a task is created, Jev also decides whether an agent could do it,
fully or as preparation. If so, 007 either runs the agent (read-only work)
or proposes it with one tap (anything that writes somewhere). The largest
new piece is reply drafts that come from the address the mail was sent to.

Builds on the Jev triage code (`agent/helpers/jev.py`, `agent/triage/`, see
`docs/jev-plan.md`). The hand-off questions ride on the same Jev call as
triage.

## Decisions and assumptions

- **Read-only executors run automatically** when Jev is confident
  (research, lookups in the user's own data). Their output lands on the
  task, and the task stays open until the user closes it.
- **Writing executors are proposed**, accepted with one tap in the
  notification or on the task: kotx issue run, calendar entry. Drafts
  are the exception and are created automatically, because nothing
  leaves the mailbox until the user presses send
  (setting `email_draft_auto`, default on).
- **No executor ever sends, merges, pays or books.** kotx keeps its own
  start/approve/merge gates. Email drafts are never sent by 007: there is
  no send code path.
- **Accept and decline are feedback.** They are the training labels for
  hand-off thresholds, since no historical labels exist.

## Hand-off kinds

| Kind | Typical tasks | Executor | Exists today | Mode | Result |
|---|---|---|---|---|---|
| `code_change` | "Fix the export bug in CSEE", GitHub mention emails asking for a change | `services/kotx/runs.create_issue_run` (CSEE / SocialAI labels) | yes, as a manual button | propose | GitHub issue + kotx run; the existing kotx flow takes over |
| `email_reply` | "Answer Anna about the meeting slot", direct questions by email | email drafts (below) | no | auto draft | draft in the mailbox of the receiving address; task closes when sent |
| `web_research` | "Find a physio near the office", "Check visa rules for Japan", "Compare train vs flight to Munich" | `agent/helpers/web.research_web` (Gemini grounded) | yes | auto | summary + sources in the description |
| `own_data_lookup` | "Find the landlord's invoice number", "Which date did we agree with Tom?", "Get the venue address" | chat agent, read-only tools (tasks, notes, messages, calendar, Drive, contacts, Notion, GitHub search) | runner yes, non-interactive entry no | auto | answer + references in the description; propose closing when it answers the task |
| `calendar_entry` | "Put the exam dates in my calendar", "Add the conference to the calendar" | `run_create_event` (after `find_calendar_events`) | yes | propose | events created; task closed |

A second question, `agent_share`, separates **prep** from **complete**.
"Prepare slides for Friday" is `own_data_lookup` + `web_research` with
`agent_share = prep`: the agent writes a brief into the description, and
the task stays with the user. Only `complete` ever proposes closing.

Not planned:
- **Payments, bookings, forms:** financial or personal-data actions, and
  no tools for them.
- **Notion / Drive writing:** those grants are read-only today.
- **Slack replies:** the user scopes are read-only.
- **Home Assistant actions:** physical side effects.

## Step H1 — Classification

Two questions added to the triage call (`app/agent/triage/questions.py`):

| Id | Type | Options |
|---|---|---|
| `handoff` | choice | none / code_change / email_reply / web_research / own_data_lookup / calendar_entry |
| `agent_share` | score | none / prep only / complete |

- Each option's text describes what the executor can actually do
  ("draft a reply email for the user to review"). Jev only sees text, not
  tools, so the descriptions are the contract. When a toolset changes,
  update the description and recalibrate.
- Eligibility is pure, in `app/services/handoff/eligible.py`. Ineligible
  kinds are removed from the options before asking:
  - `code_change`: label in `KOTX_ISSUE_REPOS` and no GitHub link yet.
  - `email_reply`: Gmail source, sender is not a no-reply address.
  - `web_research`: Gemini configured.
  - The rest: always eligible.
- Tasks created by the LLM fallback path get a single Jev call after creation.
- Thresholds: seeded by hand (strict), then refit from accept/decline in
  `services/learn/jev.refit_thresholds` (a new `handoff` decision). Run in shadow for the first
  1–2 weeks: log answers and show an unobtrusive "agent could…" chip, but
  execute nothing.

## Step H2 — Data model

Migration `0044_task_handoffs`:

| Column | Notes |
|---|---|
| `id` | uuid pk |
| `task_id` | fk → tasks, cascade |
| `kind` | one of the kinds above |
| `agent_share` | `prep` \| `complete` |
| `state` | `proposed` \| `running` \| `ready` \| `declined` \| `failed` \| `done` |
| `confidence` | Jev probability for the chosen kind |
| `result` | jsonb: `issue_url`, `draft_id`, `event_ids`, `summary`, … |
| `jev_model` | for threshold refits |
| `created_at`, `updated_at` | |

At most one non-terminal row per task. DB access goes in
`app/db/clients/handoffs.py`.

## Step H3 — Proposal surface

- `services/notify.py`: `notify_handoff_proposed(task, handoff)` with
  `ACTION_HANDOFF_ACCEPT` / `ACTION_HANDOFF_DECLINE`, following
  `notify_kotx_start`. Plus `notify_handoff_ready` for auto-run results.
- API, thin, in `app/api/tasks.py`:
  - `POST /tasks/{id}/handoff`: accept or start, optional `kind` override.
  - `POST /tasks/{id}/handoff/decline`.

  Both call `app/services/handoff/`.
- Frontend: a chip on `TaskCard` ("Agent can draft a reply"), and a
  panel in `TaskDetailModal` with the result, accept and decline. A
  "Hand off…" menu on any task lets the user pick a kind manually. A
  manual pick counts as a label.

## Step H4 — Executors

`app/services/handoff/run.py`: a dict from kind to async function, with
no base class. Each executor returns `(state, result)`. A failure sets
`failed`, calls `notify_error`, and leaves the task untouched.

- **`code_change`:** kotx needs a body. If the task has no description,
  run the triage writer (`agent/triage/write.py`) with `description` forced, then
  `create_issue_run(task)`. Store `issue_url` on `task.link`, so the
  existing "007 task created the issue" adoption path links the kotx task
  when it appears.
- **`web_research`:** `research_web(title + description)`. Append a
  "Research" section with sources to the description. State `ready`,
  plus `notify_handoff_ready`.
- **`own_data_lookup`:** new non-interactive entry in
  `agent/chat/runner.py`: `answer_task(session, task) -> str`, restricted
  to the read-only search tools (no `create_*` / `update_*`). Append the
  answer. With `agent_share = complete`, the ready notification carries
  a Close action.
- **`calendar_entry`:** one LLM call with `find_calendar_events` +
  `create_event`, reusing the handlers in `agent/tools/calendar_lookup.py`.
  Event ids go into `result`. Close the task after the events exist (the
  user already accepted).
- **`email_reply`:** see below.

Auto-run executors have a daily cap (`handoff_auto_daily_cap`) so a burst
of inputs can't fan out into dozens of research calls.

---

## Email drafts

**Constraint.** Mail sent to several addresses is forwarded into one Gmail
inbox. Replies should come from the address the mail was sent to, but
Gmail can only send as the main address.

### Step E1 — Know which address received the mail (pure)

`services/input/gmail/preprocess.py`:

- `received_as(headers, my_addresses) -> str | None`, checking in order:
  `To` ∩ mine → `Cc` ∩ mine → `X-Original-To` / `X-Forwarded-To` /
  `Delivered-To` ∩ mine → `None` (meaning the main address).
  `my_addresses` is the existing `my_email_addresses` setting plus the
  account.
- Also capture `In-Reply-To` and `References`. `Message-ID` is already
  stored as `message_id_header`.
- Store `received_as`, `in_reply_to` and `references` in `source_metadata`.
  Older rows fall back to the stored `to` / `cc`.
- Tests with fixture headers: a Workspace auto-forward, an M365 forward,
  a Gmail alias, Bcc'd delivery.

### Step E2 — Routes: where the draft is created

| Route | When | How | Scopes | Build cost |
|---|---|---|---|---|
| `gmail` | main address, and any verified Gmail "Send mail as" alias | `users.drafts.create` with `threadId`, `From` = alias, matching `Subject`, `In-Reply-To`, `References` | add `gmail.compose` | small |
| `gmail` via SMTP alias | a forwarded address whose provider allows SMTP login | **configuration, not code:** add the address under Gmail → Settings → Accounts → "Send mail as" with that provider's SMTP server + app password. It then shows up in `sendAs.list` and the `gmail` route covers it | — | none |
| `microsoft` | Microsoft 365 / Outlook addresses where SMTP login is disabled (common for university and work tenants) | new `@register_provider("microsoft")`. Graph: find the original by `internetMessageId` (forwarding keeps the Message-ID) → `createReply` → patch the body. The draft sits threaded in that mailbox; the user sends from Outlook | `Mail.ReadWrite`, `offline_access` (no `Mail.Send`) | medium |
| `imap` | any other host | IMAP `APPEND` to Drafts with `\Draft`, `In-Reply-To` / `References` set. stdlib `imaplib`, app password stored as Fernet ciphertext like OAuth tokens | — | medium |
| fallback | no route for the address | Gmail draft from the main address, clearly flagged in the UI and notification ("sends from your main address") | — | — |

- `sendAs.list` (readable with the existing `gmail.readonly` scope;
  verify) lists the verified aliases, so the `gmail` route detects them
  automatically. It also returns each alias's signature.
- Setting `email_reply_routes: dict[str, str]` (address → route) covers
  the non-Gmail routes. Unlisted addresses use `gmail` if verified,
  otherwise the fallback.
- **Decision needed before building E2:** list each forwarded address and
  its provider. The cheapest outcome is that every provider allows SMTP
  login: then they all become Gmail aliases and only the `gmail` route
  is built. Build `microsoft` / `imap` only for addresses that can't. For
  a university M365 tenant, check first whether the tenant lets users
  consent to third-party apps.

Code: `app/services/input/gmail/client.py` gains
`create_draft` / `update_draft` / `delete_draft` / `get_thread` /
`list_send_as`, but no send method. `app/services/email/route.py` (pure)
picks the route and from-address. `app/services/email/compose.py` (pure)
builds the RFC 2822 reply: `Re:` subject, threading headers, quoted
original with an "On …, … wrote:" line.

### Step E3 — Draft generation

`app/agent/draft/runner.py` + `app/agent/prompts/draft.py`, one
forced-tool call: `write_reply {body, open_questions: [str]}`.

Context:
- **The full thread:** `get_thread`, each message through the existing
  preprocess cleaning.
- **The task:** title and description.
- **`search_notes` hits** for the sender and subject.
- **Free slots** when the mail asks for a time: `find_calendar_events`
  over the next two weeks.
- **Style examples:** 3–5 recent messages the user sent to the same
  recipient, else the same domain, else any recent sent mail in the same
  language. The SENT label is readable with `gmail.readonly`.
- **Language:** the incoming mail's language.
- **Signature:** from `sendAs` (gmail route) or per-address config.

Rules:
- Facts the agent doesn't have become `[[placeholders]]` and are listed
  in `open_questions`. Those are shown in the notification and the task panel.
- **Recipients are deterministic** (pure, in `compose.py`): `Reply-To`,
  else `From`. They are never taken from the LLM or from text in the
  body, so an injected "cc this address" can't add recipients.
- The prompt treats the thread as untrusted content.
- Model: `draft_llm_model` setting. Drafting is low-volume and
  quality-sensitive, so it can be a stronger model than the Haiku
  default without a noticeable cost.

### Step E4 — Push and lifecycle

Migration `0045_email_drafts`:

| Column | Notes |
|---|---|
| `id`, `task_id`, `raw_input_id` | |
| `route`, `from_address`, `to`, `subject`, `body` | |
| `open_questions` | json |
| `provider_draft_id`, `provider_thread_ref` | Gmail draft id + thread id, Graph message id, IMAP UID |
| `state` | `generated` \| `pushed` \| `stale` \| `sent` \| `discarded` |
| `created_at`, `updated_at` | |

- **Push:** through the route. `task.link` points at the Gmail thread or
  the Outlook web link.
- **New message on the thread:** the thread follow-up branch marks the
  draft `stale` and notifies "New reply in the thread" with
  `ACTION_DRAFT_REGENERATE`. Regenerating updates the draft in place
  (`drafts.update`, Graph `PATCH`).
- **Sent detection:**
  - `gmail` route: the Gmail poll currently skips `SENT` messages. Before
    skipping, check whether the thread has a pushed draft; if so, mark
    it `sent` and close the reply task.
  - `microsoft`: a cron check of the draft id (gone, or `isDraft = false`).
  - `imap`: the user closes the task manually.
- **Discard:** `ACTION_DRAFT_DISCARD` deletes the provider draft, sets
  `discarded`, and records the hand-off as `declined`.

### Step E5 — Triggers

- At ingestion, Jev's `handoff = email_reply` creates the task with:
  - a template title, `Reply to {sender first name}: {clean subject}`
    (pure, no LLM; a consistent title style);
  - estimate 15 min;
  - no description.

  Then the draft is generated and pushed (if `email_draft_auto`), with a
  "Draft ready" notification: Open / Regenerate / Discard.
- Manually: a "Draft reply" action on any Gmail-sourced task.

### Security

- **Gmail scope:** adds `gmail.compose`. It also permits sending, so
  enforce "no send" in code: the client has no send method, and a test
  asserts that no request goes to `…/messages/send` or `…/drafts/send`.
  Users re-consent once (`prompt=consent` is already passed). Update the
  Gmail bullet in CLAUDE.md's Security section when this ships.
- **Microsoft:** `Mail.ReadWrite` only. Tokens are encrypted like the others.
- **IMAP:** app passwords, Fernet-encrypted, decrypted only at use.
- **Review:** every draft is reviewed by the user before sending.
  Recipients are deterministic.

## Rollout order

1. H1 in shadow (Jev triage must be configured), plus the chip.
2. H2, H3.
3. Executors, read-only first: `web_research`, `own_data_lookup`, then
   `code_change` (wiring only), then `calendar_entry`.
4. Email: E1 → the `gmail` route of E2 (main + verified aliases + SMTP
   aliases) → E3 → E4 → E5. Then `microsoft` / `imap`, only for the
   addresses that need them.
5. Hand-off threshold refit once accept/decline data exists.

## Risks

- **Jev judges text, not capability.** It can propose kinds the executor
  can't deliver. Mitigations: descriptive options, eligibility rules,
  propose-only for writes, decline feedback.
- **Draft voice:** style examples from the user's own sent mail, plus
  open questions instead of invented facts.
- **Tenant consent (M365):** may block the `microsoft` route entirely.
  Check before building it; the fallback still works.
- **`gmail.compose` is broader than drafts-only.** Gmail has no
  drafts-only scope; mitigated by the absent send path and the test.
- **Auto-run cost:** the daily cap, and auto-run only above a strict
  threshold.
