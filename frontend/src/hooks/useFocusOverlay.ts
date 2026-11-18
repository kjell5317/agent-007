import { useCallback, useSyncExternalStore } from "react";
import { focusClosedFor, runningSince, type FocusSession } from "@/lib/work";
import type { Task } from "@/lib/types";

// The session the focus overlay was closed on, kept across reloads so a
// closed overlay doesn't pop back up.
const STORAGE_KEY = "focus-closed";
const listeners = new Set<() => void>();
let closed: FocusSession | null = read();

function read(): FocusSession | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as FocusSession) : null;
  } catch {
    return null;
  }
}

function remember(next: FocusSession | null) {
  closed = next;
  try {
    if (next) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Without storage the overlay only forgets its closed state on reload.
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function showFocus(): void {
  if (closed) remember(null);
}

export function useFocusOverlay(task: Task | null): { open: boolean; close: () => void } {
  const current = useSyncExternalStore(subscribe, () => closed);
  const since = task ? runningSince(task) : null;
  const taskId = task?.id ?? null;
  const close = useCallback(() => {
    if (taskId && since) remember({ taskId, since });
  }, [since, taskId]);
  return { open: task !== null && since !== null && !focusClosedFor(current, task), close };
}
