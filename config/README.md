# Config

Runtime config the app reads at startup. The actual files are **personal and
git-ignored** — only the `*.example` templates and this README are tracked.

To set up (or for a fresh deploy), copy the templates and edit them:

```bash
cp config/chat_context.md.example config/chat_context.md
```

The path is overridable via `CHAT_CONTEXT_PATH` (see `app.config.settings`); it
defaults to the file in this directory.

## Labels

Labels have no config file. They are the custom event labels on the Google
calendar, mirrored into the `labels` table; the account menu → **Edit labels**
page is where each one gets the description the agent matches on and an optional
`owner/repo` for kotx coding tasks. A label with no description stays out of the
agent's choices. If no label plausibly fits an input, the agent treats that as a
signal the input isn't a task for the user.

## `chat_context.md`

Personal routing hints appended to the chat/search system prompt: which of your
projects and people live in which source (Notion / Drive / GitHub / calendar /
contacts). Lets the agent route a question to the right tool instead of
declining. Kept out of git so project names stay private; the base prompt is
committed. Missing file → base prompt only.

## Points

Points have no config file. The running score lives in the database; the
topbar shows it and lets you add/subtract manually. Completing a non-kotx task
awards `POINTS_TASK_DONE_FACTOR` × estimated minutes (set in `.env`, 0 to
disable). Completing a kotx-linked task awards a fixed `0.1` × estimated
minutes.
