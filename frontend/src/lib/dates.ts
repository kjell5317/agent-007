// The backend uses USER_TIMEZONE for notifications and planning. The frontend
// uses the same zone for calendar labels and edits, regardless of device zone.
let userTimeZone = "Europe/Berlin";

export function setUserTimezone(name: string): void {
  try {
    new Intl.DateTimeFormat("en", { timeZone: name });
    userTimeZone = name;
  } catch {
    userTimeZone = "UTC";
  }
}

export function getUserTimezone(): string {
  return userTimeZone;
}

export interface ZonedParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
}

export function zonedParts(value: string | Date): ZonedParts {
  const date = typeof value === "string" ? new Date(value) : value;
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: userTimeZone,
    hourCycle: "h23",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  }).formatToParts(date);
  const number = (type: string) => Number(parts.find((part) => part.type === type)?.value);
  return {
    year: number("year"), month: number("month"), day: number("day"),
    hour: number("hour"), minute: number("minute"),
  };
}

// Convert a wall-clock selection in USER_TIMEZONE back to an instant. Iteration
// resolves the UTC offset on the selected date, including summer/winter DST.
export function zonedWallTimeToIso(parts: ZonedParts): string {
  const target = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute);
  let instant = target;
  let nextValid: number | null = null;
  for (let i = 0; i < 5; i++) {
    const shown = zonedParts(new Date(instant));
    const shownAsUtc = Date.UTC(
      shown.year, shown.month - 1, shown.day, shown.hour, shown.minute,
    );
    const difference = target - shownAsUtc;
    if (difference === 0) return new Date(instant).toISOString();
    if (shownAsUtc > target && (nextValid === null || instant < nextValid)) {
      nextValid = instant;
    }
    instant += difference;
  }
  // A wall time in the spring DST gap has no matching instant; use the next
  // valid time rather than silently selecting the previous day or hour.
  return new Date(nextValid ?? instant).toISOString();
}

function sameZonedDay(a: ZonedParts, b: ZonedParts): boolean {
  return a.year === b.year && a.month === b.month && a.day === b.day;
}

export function isToday(iso: string | null): boolean {
  return iso !== null && sameZonedDay(zonedParts(iso), zonedParts(new Date()));
}

export function isTomorrow(iso: string | null): boolean {
  if (!iso) return false;
  const today = zonedParts(new Date());
  const next = new Date(Date.UTC(today.year, today.month - 1, today.day + 1));
  const tomorrow: ZonedParts = {
    year: next.getUTCFullYear(), month: next.getUTCMonth() + 1,
    day: next.getUTCDate(), hour: 0, minute: 0,
  };
  return sameZonedDay(zonedParts(iso), tomorrow);
}

export function isOverdue(iso: string | null): boolean {
  if (!iso) return false;
  return new Date(iso).getTime() < Date.now();
}

export function isUrgent(
  iso: string | null,
  estimationMinutes: number | null,
): boolean {
  if (!iso || estimationMinutes == null) return false;
  const due = new Date(iso).getTime();
  const now = Date.now();
  if (due <= now) return false;
  return now >= due - estimationMinutes * 60_000 * 1.5;
}

export function dueDateBadgeVariant(
  iso: string | null,
  estimationMinutes: number | null,
): "overdue" | "urgent" | "closed" {
  if (!iso) return "closed";
  const due = new Date(iso).getTime();
  if (Number.isNaN(due)) return "closed";
  const now = Date.now();
  if (estimationMinutes != null && due - estimationMinutes * 60_000 - now < 0) {
    return "overdue";
  }
  if (due > now && due - now < 24 * 60 * 60 * 1000) return "urgent";
  return "closed";
}

export function fmtDue(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const time = d.toLocaleTimeString(undefined, {
    timeZone: userTimeZone, hour: "2-digit", minute: "2-digit",
  });
  if (isToday(iso)) return `Today ${time}`;
  if (isTomorrow(iso)) return `Tomorrow ${time}`;
  return d.toLocaleString(undefined, {
    timeZone: userTimeZone,
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function fmtWhen(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const sameYear = zonedParts(d).year === zonedParts(new Date()).year;
  return d.toLocaleString(undefined, {
    timeZone: userTimeZone,
    month: "short", day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
    hour: "2-digit", minute: "2-digit",
  });
}
