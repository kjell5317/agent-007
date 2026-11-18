import { Card, CardContent } from "@/components/ui/card";
import { WorkControls } from "@/components/work/WorkControls";
import { useWorkActions } from "@/components/work/useWorkActions";
import { useLabels } from "@/hooks/useLabels";
import { useSecondClock } from "@/hooks/useSecondClock";
import { labelChipStyle } from "@/lib/labels";
import { elapsedMs, fmtElapsed, fmtMinutes, runningSince, workedMinutes } from "@/lib/work";
import type { Task } from "@/lib/types";

// The running task, and nothing about how long it "should" take: the clock
// only counts up.
export function FocusPanel({
  task,
  onChanged,
  onOpen,
}: {
  task: Task;
  onChanged: () => Promise<void> | void;
  onOpen: (id: string) => void;
}) {
  const now = useSecondClock();
  const work = useWorkActions(task, { onChanged });
  const labelMeta = useLabels().find((l) => l.name === task.label);
  const since = runningSince(task);

  return (
    <Card className="border-primary/50">
      <CardContent className="space-y-5 p-4">
        <div className="space-y-1.5">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            <span className="h-2 w-2 animate-pulse rounded-full bg-primary" aria-hidden="true" />
            Working on
          </div>
          <button
            type="button"
            onClick={() => onOpen(task.id)}
            className="block w-full break-words text-left text-xl font-semibold leading-tight"
          >
            {task.title}
          </button>
          {task.label && (
            <span
              className="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground"
              style={labelChipStyle(labelMeta?.color)}
            >
              {task.label}
            </span>
          )}
        </div>
        <div className="text-center">
          <div role="timer" aria-label="Time on this session" className="text-5xl font-semibold tabular-nums">
            {fmtElapsed(since ? elapsedMs(since, now) : 0)}
          </div>
          {task.worked_minutes > 0 && (
            <div className="mt-1 text-xs text-muted-foreground">
              {fmtMinutes(workedMinutes(task, now))} in total
            </div>
          )}
        </div>
        <WorkControls work={work} />
      </CardContent>
      {work.dialogs}
    </Card>
  );
}
