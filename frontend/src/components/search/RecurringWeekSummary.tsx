import { useMemo } from "react";
import { worstCaseWeek } from "@/lib/recurrence";
import type { Task } from "@/lib/types";
import { fmtMinutes } from "@/lib/work";

export function RecurringWeekSummary({ tasks }: { tasks: Task[] }) {
  const week = useMemo(() => worstCaseWeek(tasks), [tasks]);
  return (
    <p className="px-1 text-xs font-medium text-muted-foreground">
      Worst-case week: <span className="text-foreground">{fmtMinutes(week.minutes)}</span>
      {" "}across {week.series} recurring {week.series === 1 ? "task" : "tasks"}
      {week.unestimated > 0 && `, ${week.unestimated} without an estimate`}
    </p>
  );
}
