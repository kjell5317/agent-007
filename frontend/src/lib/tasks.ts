import type { Task } from "@/lib/types";

function timeOrNull(iso: string | null): number | null {
  if (!iso) return null;
  const time = new Date(iso).getTime();
  return Number.isNaN(time) ? null : time;
}

export function compareTasksBySchedule(a: Task, b: Task): number {
  const as = timeOrNull(a.scheduled_date);
  const bs = timeOrNull(b.scheduled_date);
  if (as != null && bs != null && as !== bs) return as - bs;
  if (as != null && bs == null) return -1;
  if (as == null && bs != null) return 1;

  if (as == null && bs == null) {
    const ad = timeOrNull(a.due_date);
    const bd = timeOrNull(b.due_date);
    if (ad != null && bd != null && ad !== bd) return ad - bd;
    if (ad != null && bd == null) return -1;
    if (ad == null && bd != null) return 1;
  }

  return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
}

export function taskGroupDate(task: Task): string | null {
  return task.scheduled_date ?? task.due_date ?? null;
}
