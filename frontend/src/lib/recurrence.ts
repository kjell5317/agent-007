import { taskRecurrence } from "@/lib/tasks";
import type { RecurrenceUnit, Task } from "@/lib/types";

// The most times a rule can land inside one week: every N days fits
// ceil(7 / N) times; a weekly, monthly or yearly rule at most once.
export function worstCaseWeeklyCount(unit: RecurrenceUnit, interval: number): number {
  return unit === "day" ? Math.ceil(7 / interval) : 1;
}

export interface RecurringWeek {
  minutes: number;
  series: number;
  // Series whose occurrences carry no estimate at all, so add nothing.
  unestimated: number;
}

interface Occurrence {
  minutes: number;
  estimated: boolean;
}

// The open instances of a series repeat one rule, so a series counts once, at
// its costliest occurrence. A split occurrence costs the sum of its subtasks,
// which inherit the occurrence's rule.
export function worstCaseWeek(tasks: readonly Task[]): RecurringWeek {
  const series = new Map<string, { perWeek: number; occurrences: Map<string, Occurrence> }>();
  for (const task of tasks) {
    const rule = taskRecurrence(task);
    if (!rule) continue;
    const owner = rule.inherited ? task.parent_card! : task;
    const key = owner.recurrence_series_id ?? owner.id;
    let entry = series.get(key);
    if (!entry) {
      entry = { perWeek: worstCaseWeeklyCount(rule.unit, owner.recurrence_interval!), occurrences: new Map() };
      series.set(key, entry);
    }
    const occurrence = entry.occurrences.get(owner.id) ?? { minutes: 0, estimated: false };
    occurrence.minutes += task.estimation ?? 0;
    occurrence.estimated ||= task.estimation != null;
    entry.occurrences.set(owner.id, occurrence);
  }
  let minutes = 0;
  let unestimated = 0;
  for (const { perWeek, occurrences } of series.values()) {
    const all = [...occurrences.values()];
    minutes += perWeek * Math.max(...all.map((occurrence) => occurrence.minutes));
    if (!all.some((occurrence) => occurrence.estimated)) unestimated += 1;
  }
  return { minutes, series: series.size, unestimated };
}
