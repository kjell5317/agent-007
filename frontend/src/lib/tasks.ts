import type { DeadlineKind, Effort, RecurrenceUnit, Task, WorkType } from "@/lib/types";

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

// A new task reads "pending" from creation until its first solve places it
// or gives up. Parents read "pending" for good, so they never count.
export function isPlanning(
  task: Pick<Task, "schedule_status" | "schedule_state" | "is_container">,
): boolean {
  return task.schedule_status === "pending" && !task.is_container && task.schedule_state !== "waiting";
}

// The shopping task comes from the grocery planner, not from a message or
// the composer.
export function isGroceryTask(task: Pick<Task, "raw_inputs">): boolean {
  return task.raw_inputs.some((input) => input.source === "grocery");
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

export const WORK_TYPE_LABEL: Record<WorkType, string> = {
  physical: "Physical",
  cognitive: "Cognitive",
  social: "Social",
};

export const EFFORT_LABEL: Record<Effort, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
};

export const DEADLINE_KIND_LABEL: Record<DeadlineKind, string> = {
  hard: "Hard",
  soft: "Soft",
};

export const DEADLINE_KIND_HINT: Record<DeadlineKind, string> = {
  hard: "Must be met, lateness has consequences",
  soft: "A target, lateness is cheap",
};

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

// Deferred tasks have no slot yet, so the schedule order would float them to
// the top; they only matter once everything planned is out of the way.
export function compareTasks(a: Task, b: Task, mode: TaskSortMode): number {
  const deferredOrder = Number(a.schedule_status === "deferred") - Number(b.schedule_status === "deferred");
  if (deferredOrder !== 0) return deferredOrder;
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
