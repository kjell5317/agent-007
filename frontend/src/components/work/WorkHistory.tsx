import { useEffect, useState } from "react";
import { useMinuteClock } from "@/hooks/useMinuteClock";
import { api } from "@/lib/api";
import { fmtClock, fmtTimeOfDay } from "@/lib/dates";
import { fmtMinutes, sessionLabel, sessionMinutes, workedMinutes } from "@/lib/work";
import type { Task, WorkSession } from "@/lib/types";

export function WorkHistory({ task }: { task: Task }) {
  const now = useMinuteClock();
  const [sessions, setSessions] = useState<WorkSession[]>([]);

  useEffect(() => {
    let cancelled = false;
    api.listWorkSessions(task.id)
      .then((rows) => { if (!cancelled) setSessions(rows); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [task.id, task.status, task.running_since, task.worked_minutes]);

  if (sessions.length === 0) return null;

  return (
    <section className="space-y-2">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold">Work</h3>
        <span className="text-sm tabular-nums text-muted-foreground">
          {fmtMinutes(workedMinutes(task, now))} worked
        </span>
      </div>
      <ul className="divide-y rounded-lg border text-sm">
        {sessions.map((session) => (
          <li key={session.id} className="flex items-center justify-between gap-3 px-3 py-2">
            <span className="min-w-0 truncate tabular-nums">
              {fmtClock(session.started_at)} – {session.ended_at ? fmtTimeOfDay(session.ended_at) : "now"}
            </span>
            <span className="shrink-0 text-xs text-muted-foreground">
              {fmtMinutes(sessionMinutes(session, now))} · {sessionLabel(session)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
