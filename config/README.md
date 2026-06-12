# Config

Runtime config the app reads at startup. The actual files are **personal and
git-ignored** — only the `*.example` templates and this README are tracked.

To set up (or for a fresh deploy), copy the templates and edit them:

```bash
cp config/labels.toml.example config/labels.toml
cp config/points.yaml.example config/points.yaml
```

Paths are overridable via `LABELS_CONFIG_PATH` / `POINTS_CONFIG_PATH` (see
`app.config.settings`); they default to the files in this directory.

## `labels.toml`

The set of task labels the agent may assign. Each label has a `description`
(shown to the agent so it can pick a fit) and a `color` (Google Calendar
event `colorId`, 1–11). If no label plausibly fits an input, the agent treats
that as a signal the input isn't a task for the user.

## `points.yaml`

The actions shown on the Points page, grouped into `sport` / `nutrition` /
`other`. Each action has a `name` and `factor` (points per unit); an optional
`unit` turns the entry into a number input. `task_done_factor` is points per
estimated minute awarded automatically on task completion (0 to disable).
