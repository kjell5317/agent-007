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

export function TaskCard({ task, onChanged, onOpen, unseen = false, onVisible }: Props) {
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
      ref={cardRef}
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
              className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"
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
