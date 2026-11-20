import { useCallback, useEffect, useMemo, useSyncExternalStore } from "react";
import { breakOver, focusTarget, runningSince, type FocusSession } from "@/lib/work";
import type { Task } from "@/lib/types";

// Kept across reloads: the session the overlay was closed on, so a closed
// overlay doesn't pop back up, and a break taken from the overlay, so it
// stays up through the break.
const CLOSED_KEY = "focus-closed";
const BREAK_KEY = "focus-break";

interface FocusState {
  closed: FocusSession | null;
  onBreak: FocusSession | null;
}

const listeners = new Set<() => void>();
let state: FocusState = { closed: load(CLOSED_KEY), onBreak: load(BREAK_KEY) };

function load(key: string): FocusSession | null {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as FocusSession) : null;
  } catch {
    return null;
  }
}

function save(key: string, value: FocusSession | null) {
  try {
    if (value) window.localStorage.setItem(key, JSON.stringify(value));
    else window.localStorage.removeItem(key);
  } catch {
    // Without storage the overlay only forgets its state on reload.
  }
}

function update(next: Partial<FocusState>) {
  if (next.closed !== undefined && next.closed !== state.closed) save(CLOSED_KEY, next.closed);
  if (next.onBreak !== undefined && next.onBreak !== state.onBreak) save(BREAK_KEY, next.onBreak);
  state = { ...state, ...next };
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function showFocus(): void {
  if (state.closed || state.onBreak) update({ closed: null, onBreak: null });
}

export function takeBreak(taskId: string): void {
  update({ onBreak: { taskId, since: new Date().toISOString() } });
}

export function useFocusOverlay(tasks: Task[]): {
  task: Task | null;
  breakSince: string | null;
  close: () => void;
} {
  const { closed, onBreak } = useSyncExternalStore(subscribe, () => state);
  const target = useMemo(() => focusTarget(tasks, closed, onBreak), [tasks, closed, onBreak]);

  // A start made anywhere else (another device, a notification) ends the break.
  useEffect(() => {
    if (onBreak && breakOver(onBreak, tasks)) update({ onBreak: null });
  }, [onBreak, tasks]);

  const task = target?.task ?? null;
  const taskId = task?.id ?? null;
  const since = task ? runningSince(task) : null;
  const breakSince = target?.breakSince ?? null;
  const close = useCallback(() => {
    if (breakSince) update({ onBreak: null });
    else if (taskId && since) update({ closed: { taskId, since } });
  }, [breakSince, since, taskId]);
  return { task, breakSince, close };
}
