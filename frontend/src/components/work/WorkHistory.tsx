import { useEffect, useState } from "react";
import { MetaDot } from "@/components/inbox/InboxCard";
import { DisclosureSection } from "@/components/ui/disclosure-section";
import { useNow } from "@/hooks/useNow";
import { api } from "@/lib/api";
import { fmtClock, fmtTimeOfDay } from "@/lib/dates";
import { fmtMinutes, sessionLabel, sessionMinutes, workedMinutes } from "@/lib/work";
import type { Task, WorkSession } from "@/lib/types";

export function WorkHistory({ task }: { task: Task }) {
  const now = useNow(60_000);
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
    <DisclosureSection
      title={<>Work log ({sessions.length}) · <span className="tabular-nums">{fmtMinutes(workedMinutes(task, now))}</span></>}
    >
      {sessions.map((session) => (
        <div key={session.id} className="rounded-lg border p-3">
          <div className="truncate text-sm font-medium tabular-nums">
            {fmtClock(session.started_at)} – {session.ended_at ? fmtTimeOfDay(session.ended_at) : "now"}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <span className="font-medium tabular-nums">{fmtMinutes(sessionMinutes(session, now))}</span>
            <MetaDot />
            <span className="font-medium">{sessionLabel(session)}</span>
          </div>
        </div>
      ))}
    </DisclosureSection>
  );
}
