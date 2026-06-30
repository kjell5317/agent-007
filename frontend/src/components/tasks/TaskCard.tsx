import { useState } from "react";
import {
  Circle,
  CircleCheckBig,
  MapPin,
  Timer,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { TaskDetailModal } from "@/components/tasks/TaskDetailModal";
import { useLabels } from "@/hooks/useLabels";
import { api } from "@/lib/api";
import { fmtDue, isOverdue, isUrgent } from "@/lib/dates";
import { labelChipClass } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { Task } from "@/lib/types";

interface Props {
  task: Task;
  onChanged: () => Promise<void> | void;
  seenAfter: string | null;
}

const CROSS_OFF_MS = 350;

export function TaskCard({ task, onChanged, seenAfter }: Props) {
  const [busy, setBusy] = useState(false);
  const [crossing, setCrossing] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const labels = useLabels();

  const scheduledOverdue = isOverdue(task.scheduled_date);
  const scheduledUrgent = isUrgent(task.scheduled_date, task.estimation);
  const labelMeta = labels.find((l) => l.name === task.label);
  // Manual tasks are excluded from the Tasks-tab unread count on the
  // server (count_since skips manual-only tasks). Suppress the per-card
  // dot too so the badge and the dots stay consistent.
  const unread =
    seenAfter !== null &&
    !task.is_manual &&
    new Date(task.created_at).getTime() > new Date(seenAfter).getTime();

  async function withBusy<T>(fn: () => Promise<T>, msg: string) {
    setBusy(true);
    try {
      await fn();
      toast.success(msg);
      await onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const crossOff = () => {
    if (crossing || busy) return;
    setCrossing(true);
    setTimeout(() => {
      withBusy(() => api.closeTask(task.id), "Marked done");
    }, CROSS_OFF_MS);
  };

  return (
    <Card
      className={cn(
        "transition-opacity duration-300",
        crossing && "pointer-events-none opacity-40",
      )}
    >
      <CardContent
        className="cursor-pointer"
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button,a,summary")) return;
          setDetailOpen(true);
        }}
      >
        <div className="flex items-center gap-2">
          <IconButton
            label="Mark done"
            disabled={busy || crossing}
            onClick={crossOff}
            className="text-muted-foreground hover:text-primary"
          >
            {crossing ? (
              <CircleCheckBig className="h-5 w-5 text-primary" />
            ) : (
              <Circle className="h-5 w-5" />
            )}
          </IconButton>
          <div className="flex min-w-0 flex-1 flex-col">
            <div className="flex items-center gap-2">
              {unread && (
                <span
                  aria-label="Unread"
                  title="Unread"
                  className="inline-block h-2 w-2 shrink-0 rounded-full bg-emerald-500"
                />
              )}
              <span
                className={cn(
                  "min-w-0 flex-1 truncate font-medium leading-snug transition-all duration-300",
                  crossing && "line-through opacity-60",
                )}
              >
                {task.title}
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              {task.scheduled_date && (
                <Badge
                  variant={scheduledOverdue ? "overdue" : scheduledUrgent ? "urgent" : "open"}
                >
                  {fmtDue(task.scheduled_date)}
                </Badge>
              )}

              {task.label && (
                <span
                  title={labelMeta?.description ?? task.label}
                  className={cn(
                    "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium",
                    labelChipClass(labelMeta?.color),
                  )}
                >
                  {task.label}
                </span>
              )}

              {task.estimation != null && (
                <span className="inline-flex items-center gap-1">
                  <Timer className="h-3 w-3" />
                  {task.estimation} min
                </span>
              )}
              {task.location && (
                <span
                  className="inline-flex items-center gap-1"
                  title={task.location}
                >
                  <MapPin className="h-3 w-3" />
                  {task.location.length > 10
                    ? `${String(task.location).charAt(0).toUpperCase() + String(task.location).slice(1, 10)}...`
                    : String(task.location).charAt(0).toUpperCase() +
                      String(task.location).slice(1)}
                </span>
              )}
            </div>
          </div>

          <IconButton
            label="Mark not a task"
            disabled={busy || crossing}
            onClick={() =>
              withBusy(() => api.markNotTask(task.id), "Marked not a task")
            }
            className="text-muted-foreground hover:text-destructive"
          >
            <Trash2 className="h-4 w-4" />
          </IconButton>
        </div>
      </CardContent>

      {detailOpen && (
        <TaskDetailModal
          task={task}
          onClose={() => setDetailOpen(false)}
          onChanged={onChanged}
        />
      )}
    </Card>
  );
}

function IconButton({
  label,
  children,
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md transition-colors disabled:pointer-events-none disabled:opacity-50",
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
}
