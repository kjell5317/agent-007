import type { RawInput, Task } from "./types";

/**
 * One shared `EventSource` for the whole app, replacing task / inbox polling.
 *
 * The connection is opened lazily on the first subscriber and kept open for
 * the app's lifetime (it's cheap, and tearing it down on every transient
 * 0-subscriber moment would just cause reconnect churn). `EventSource`
 * reconnects on its own after a drop; consumers reconcile any events missed
 * while disconnected via the focus/visibility refetch in `useAppData`.
 */

export type ServerEvent =
  | { type: "task"; data: Task }
  | { type: "task_removed"; id: string }
  | { type: "input"; data: RawInput }
  | { type: "points"; total: number }
  // A kotx run changed upstream (webhook or reconciliation poll landed) —
  // no payload; subscribers refetch /kotx/tasks.
  | { type: "kotx" };

type Handler = (event: ServerEvent) => void;

const handlers = new Set<Handler>();
let source: EventSource | null = null;

function ensureOpen() {
  if (source) return;
  source = new EventSource("/events");
  source.onmessage = (ev) => {
    let parsed: ServerEvent;
    try {
      parsed = JSON.parse(ev.data);
    } catch {
      return;
    }
    handlers.forEach((h) => h(parsed));
  };
}

export function subscribeEvents(handler: Handler): () => void {
  handlers.add(handler);
  ensureOpen();
  return () => {
    handlers.delete(handler);
  };
}
