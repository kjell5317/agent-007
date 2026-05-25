export function isToday(iso: string | null): boolean {
  if (!iso) return false;
  const d = new Date(iso);
  const now = new Date();
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
}

export function isOverdue(iso: string | null): boolean {
  if (!iso) return false;
  // Past the due instant counts as overdue — including same-day times that
  // have already gone by (e.g. due at 09:00 when it's now 17:00).
  return new Date(iso).getTime() < Date.now();
}

// "Urgent" = within 1.5× the estimation of the deadline but not yet overdue.
// A 30-minute task due at 10:00 turns urgent at 09:15. Without an estimation
// we can't compute the threshold, so the badge stays in its normal state.
export function isUrgent(
  iso: string | null,
  estimationMinutes: number | null,
): boolean {
  if (!iso || estimationMinutes == null) return false;
  const due = new Date(iso).getTime();
  const now = Date.now();
  if (due <= now) return false; // overdue, not urgent
  const threshold = due - estimationMinutes * 60_000 * 1.5;
  return now >= threshold;
}

export function fmtDue(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  // Always show the local time alongside the date — otherwise a deadline
  // of "tomorrow 17:00" reads as just "May 26" and loses the hour.
  return d.toLocaleString(undefined, {
    ...(isToday(iso) ? {} : { month: "short", day: "numeric" }),
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtWhen(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
    hour: "2-digit",
    minute: "2-digit",
  });
}
