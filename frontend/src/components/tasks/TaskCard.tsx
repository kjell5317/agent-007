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
import { useLabels } from "@/hooks/useLabels";
import { api } from "@/lib/api";
import { fmtDue, isOverdue, isUrgent } from "@/lib/dates";
import { labelChipClass } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { Task } from "@/lib/types";

interface Props {
  task: Task;
  onChanged: () => Promise<void> | void;
  onOpen: (id: string) => void;
}

const CROSS_OFF_MS = 350;

export function TaskCard({ task, onChanged, onOpen }: Props) {
  const [busy, setBusy] = useState(false);
  const [crossing, setCrossing] = useState(false);
  const labels = useLabels();

  const displayDate = task.scheduled_date ?? task.due_date;
  const displayOverdue = isOverdue(displayDate);
  const displayUrgent = isUrgent(displayDate, task.estimation);
  const labelMeta = labels.find((l) => l.name === task.label);

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
          onOpen(task.id);
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
              {displayDate && (
                <Badge
                  variant={displayOverdue ? "overdue" : displayUrgent ? "urgent" : "open"}
                >
                  {fmtDue(displayDate)}
                </Badge>
              )}

              {task.label && (
                <span
                  title={labelMeta?.description ?? task.label}
                  className={cn(
                    "inline-flex items-center rounded-full border border-transparent px-2 py-0.5 text-xs font-medium",
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
