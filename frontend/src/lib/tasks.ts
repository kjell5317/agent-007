import type { Task } from "@/lib/types";

export type TaskSortMode = "scheduled" | "due";

function timeOrNull(iso: string | null): number | null {
  if (!iso) return null;
  const time = new Date(iso).getTime();
  return Number.isNaN(time) ? null : time;
}

function compareNullableAsc(a: number | null, b: number | null): number {
  if (a != null && b != null && a !== b) return a - b;
  if (a != null && b == null) return -1;
  if (a == null && b != null) return 1;
  return 0;
}

function compareCreatedNewestFirst(a: Task, b: Task): number {
  return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
}

export function compareTasksBySchedule(a: Task, b: Task): number {
  const as = timeOrNull(a.scheduled_date);
  const bs = timeOrNull(b.scheduled_date);
  if (as == null && bs != null) return -1;
  if (as != null && bs == null) return 1;
  if (as != null && bs != null && as !== bs) return as - bs;

  if (as == null && bs == null) {
    const ad = timeOrNull(a.due_date);
    const bd = timeOrNull(b.due_date);
    const dueOrder = compareNullableAsc(ad, bd);
    if (dueOrder !== 0) return dueOrder;
  }

  return compareCreatedNewestFirst(a, b);
}

export function compareTasksByDue(a: Task, b: Task): number {
  const ad = timeOrNull(a.due_date);
  const bd = timeOrNull(b.due_date);
  const dueOrder = compareNullableAsc(ad, bd);
  if (dueOrder !== 0) return dueOrder;

  if (ad == null && bd == null) {
    const scheduledOrder = compareNullableAsc(
      timeOrNull(a.scheduled_date),
      timeOrNull(b.scheduled_date),
    );
    if (scheduledOrder !== 0) return scheduledOrder;
  }

  return compareCreatedNewestFirst(a, b);
}

export function compareTasks(a: Task, b: Task, mode: TaskSortMode): number {
  return mode === "due" ? compareTasksByDue(a, b) : compareTasksBySchedule(a, b);
}

export function taskGroupDate(
  task: Task,
  mode: TaskSortMode = "scheduled",
): string | null {
  return mode === "due"
    ? task.due_date ?? task.scheduled_date ?? null
    : task.scheduled_date ?? task.due_date ?? null;
}
