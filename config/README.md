# Config

Runtime config the app reads at startup. The actual files are **personal and
git-ignored** — only the `*.example` templates and this README are tracked.

To set up (or for a fresh deploy), copy the template and edit it:

```bash
cp config/labels.toml.example config/labels.toml
```

The path is overridable via `LABELS_CONFIG_PATH` (see `app.config.settings`);
it defaults to the file in this directory.

## `labels.toml`

The set of task labels the agent may assign. Each label has a `description`
(shown to the agent so it can pick a fit) and a `color` (Google Calendar
event `colorId`, 1–11). If no label plausibly fits an input, the agent treats
that as a signal the input isn't a task for the user.

## Points

Points have no config file. The running score lives in the database; the
topbar shows it and lets you add/subtract manually. Completing a non-kotx task
awards `POINTS_TASK_DONE_FACTOR` × estimated minutes (set in `.env`, 0 to
disable). Completing a kotx-linked task awards a fixed `0.1` × estimated
minutes.
