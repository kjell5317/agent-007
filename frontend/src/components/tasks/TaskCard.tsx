import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  Circle,
  CircleCheckBig,
  MapPin,
  Timer,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { actionHint } from "@/components/runs/runLabels";
import { useLabels } from "@/hooks/useLabels";
import { api } from "@/lib/api";
import { fmtDue, isOverdue, isUrgent } from "@/lib/dates";
import { kotx, type KotxTask } from "@/lib/kotx";
import { labelChipClass } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { Task } from "@/lib/types";

interface Props {
  task: Task;
  kotxTask?: KotxTask | null;
  onChanged: () => Promise<void> | void;
  onKotxChanged: () => Promise<void> | void;
  onOpen: (id: string) => void;
  unseen?: boolean;
  onVisible?: (id: string) => void;
}

const CROSS_OFF_MS = 350;
const LOCATION_WRAP_TOLERANCE_PX = 1;

function formatTaskCardLocation(location: string | null) {
  if (!location) return null;

  const formatted =
    location.charAt(0).toUpperCase() +
    (location.length > 10 ? `${location.slice(1, 10)}...` : location.slice(1));

  return formatted;
}

export function TaskCard({
  task,
  kotxTask = null,
  onChanged,
  onKotxChanged,
  onOpen,
  unseen = false,
  onVisible,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [crossing, setCrossing] = useState(false);
  const [locationVisible, setLocationVisible] = useState(true);
  const [measurementVersion, setMeasurementVersion] = useState(0);
  const cardRef = useRef<HTMLDivElement>(null);
  const metadataRef = useRef<HTMLDivElement>(null);
  const locationRef = useRef<HTMLSpanElement>(null);
  const lastMeasuredWidthRef = useRef<number | null>(null);
  const lastProbeKeyRef = useRef<string | null>(null);
  const labels = useLabels();

  const kotxAction = kotxTask ? actionHint(kotxTask) : null;
  const displayDate = task.scheduled_date ?? task.due_date;
  const displayOverdue = isOverdue(displayDate);
  const displayUrgent = isUrgent(displayDate, task.estimation);
  const labelMeta = labels.find((l) => l.name === task.label);
  const displayLocation = formatTaskCardLocation(task.location);
  const locationProbeKey = [
    displayDate ?? "",
    task.estimation ?? "",
    task.label ?? "",
    displayLocation ?? "",
    measurementVersion,
  ].join("|");

  useLayoutEffect(() => {
    if (!displayLocation) return;

    if (!locationVisible) {
      if (lastProbeKeyRef.current !== locationProbeKey) {
        lastProbeKeyRef.current = locationProbeKey;
        setLocationVisible(true);
      }
      return;
    }

    const metadata = metadataRef.current;
    const location = locationRef.current;
    if (!metadata || !location) return;

    lastProbeKeyRef.current = locationProbeKey;

    const metadataTop = metadata.getBoundingClientRect().top;
    const locationTop = location.getBoundingClientRect().top;
    const nextVisible =
      locationTop <= metadataTop + LOCATION_WRAP_TOLERANCE_PX;

    setLocationVisible((prev) => (prev === nextVisible ? prev : nextVisible));
  }, [displayLocation, locationProbeKey, locationVisible]);

  useEffect(() => {
    if (!unseen || !onVisible) return;
    const node = cardRef.current;
    if (!node) return;

    if (typeof IntersectionObserver === "undefined") {
      onVisible(task.id);
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        onVisible(task.id);
        observer.disconnect();
      },
      { threshold: 0.5 },
    );

    observer.observe(node);
    return () => observer.disconnect();
  }, [onVisible, task.id, unseen]);

  useEffect(() => {
    const metadata = metadataRef.current;
    if (!metadata || !displayLocation) return;

    const updateMeasuredWidth = (width: number) => {
      const lastWidth = lastMeasuredWidthRef.current;
      if (lastWidth != null && Math.abs(lastWidth - width) < 0.5) return;

      lastMeasuredWidthRef.current = width;
      setMeasurementVersion((version) => version + 1);
    };

    updateMeasuredWidth(metadata.getBoundingClientRect().width);

    if (typeof ResizeObserver === "undefined") {
      const onResize = () =>
        updateMeasuredWidth(metadata.getBoundingClientRect().width);
      window.addEventListener("resize", onResize);
      return () => window.removeEventListener("resize", onResize);
    }

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      updateMeasuredWidth(entry.contentRect.width);
    });

    observer.observe(metadata);
    return () => observer.disconnect();
  }, [displayLocation]);

  async function withBusy<T>(
    fn: () => Promise<T>,
    msg: string,
    afterChanged: () => Promise<void> | void = onChanged,
  ) {
    setBusy(true);
    try {
      await fn();
      toast.success(msg);
      await afterChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // On kotx cards the check-off dismisses (discards the run — done is handled
  // through kotx), but keeps the same icon and animation as every other task.
  const crossOff = () => {
    if (crossing || busy) return;
    setCrossing(true);
    setTimeout(() => {
      if (kotxTask) {
        if (kotxTask.canDiscard) {
          withBusy(
            () => kotx.discard(kotxTask.id),
            "Run discarded",
            onKotxChanged,
          );
        } else {
          withBusy(() => api.markNotTask(task.id), "Marked not a task");
        }
      } else {
        withBusy(() => api.closeTask(task.id), "Marked done");
      }
    }, CROSS_OFF_MS);
  };

  return (
    <Card
      ref={cardRef}
      className={cn(
        "transition-opacity duration-300",
        task.kotx_task_id != null && "border-primary/50",
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
            label={
              kotxTask
                ? kotxTask.canDiscard
                  ? "Dismiss run"
                  : "Mark not a task"
                : "Mark done"
            }
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
              {unseen && (
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
            <div
              ref={metadataRef}
              className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground"
            >
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
              {displayLocation && locationVisible && (
                <span
                  ref={locationRef}
                  className="inline-flex items-center gap-1"
                  title={task.location ?? undefined}
                >
                  <MapPin className="h-3 w-3" />
                  {displayLocation}
                </span>
              )}
            </div>
          </div>

          {kotxTask ? (
            // The pending run action opens the modal (where the doc + action
            // buttons live) rather than acting directly — a one-tap merge is
            // too risky.
            kotxAction && (
              <Button
                size="sm"
                className="shrink-0"
                disabled={busy || crossing}
                onClick={() => onOpen(task.id)}
              >
                {kotxAction}
              </Button>
            )
          ) : (
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
          )}
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
