"""Push notifications.

* `push` — sends a `Message` through the configured provider (ntfy or the
  Home Assistant companion app); `ntfy` is that provider's transport.
* `messages` — action ids, tags and one pure builder per notification kind.
* `notify` — event notifications that fire once at a moment: errors, a new
  task, a result an agent produced, the start cue.
* `reconcile` + `settle` — state notifications that describe the plan (past
  deadline, commute conflict, commute without a route). They go out only once
  scheduling has settled, diffed against the ledger of what is showing.
"""
