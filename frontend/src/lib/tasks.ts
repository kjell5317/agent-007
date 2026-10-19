import type { RecurrenceUnit, Task } from "@/lib/types";

export type TaskSortMode = "scheduled" | "due";

// Each unit stops where the next one takes over — mirrors the backend's
// `MAX_INTERVAL` (a week has 7 days, a month ~4 weeks, a year 12 months).
export const RECURRENCE_MAX: Record<RecurrenceUnit, number> = { day: 6, week: 4, month: 12 };

const RECURRENCE_ADVERB: Record<RecurrenceUnit, string> = {
  day: "Daily",
  week: "Weekly",
  month: "Monthly",
};

export function fmtRecurrence(unit: RecurrenceUnit, interval: number): string {
  return interval === 1 ? RECURRENCE_ADVERB[unit] : `Every ${interval} ${unit}s`;
}

// Splitting a repeating occurrence only splits that occurrence: its subtasks
// don't repeat themselves, they show the rule of the occurrence they belong to.
export function taskRecurrence(
  task: Pick<Task, "recurrence_unit" | "recurrence_interval" | "parent_card">,
): { label: string; unit: RecurrenceUnit; inherited: boolean } | null {
  if (task.recurrence_unit && task.recurrence_interval) {
    return { label: fmtRecurrence(task.recurrence_unit, task.recurrence_interval), unit: task.recurrence_unit, inherited: false };
  }
  const parent = task.parent_card;
  if (parent?.recurrence_unit && parent.recurrence_interval) {
    return { label: fmtRecurrence(parent.recurrence_unit, parent.recurrence_interval), unit: parent.recurrence_unit, inherited: true };
  }
  return null;
}

export function isTaskOverdue(task: Task, now = Date.now()): boolean {
  if (task.status !== "open") return false;
  const due = timeOrNull(task.due_date);
  const scheduled = timeOrNull(task.scheduled_date);
  const duration = Math.max(5, task.estimation || 30) * 60_000;
  return (due != null && due < now) ||
    (scheduled != null && scheduled + duration < now);
}

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
