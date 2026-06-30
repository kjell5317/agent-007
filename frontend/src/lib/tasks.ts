import type { Task } from "@/lib/types";

function taskSortTime(task: Task): number {
  const iso = task.scheduled_date ?? task.due_date;
  return iso ? new Date(iso).getTime() : Infinity;
}

export function compareTasksBySchedule(a: Task, b: Task): number {
  const at = taskSortTime(a);
  const bt = taskSortTime(b);
  if (at !== bt) return at - bt;
  return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
}

export function taskGroupDate(task: Task): string | null {
  return task.scheduled_date ?? task.due_date ?? null;
}
