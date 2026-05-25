import { useState } from "react";
import {
  Circle,
  CircleCheckBig,
  MapPin,
  Settings,
  Timer,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { useLabels } from "@/hooks/useLabels";
import { api } from "@/lib/api";
import { fmtDue, isOverdue, isUrgent } from "@/lib/dates";
import { labelChipClass } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { AiDoable, Label, Task } from "@/lib/types";

interface Props {
  task: Task;
  onChanged: () => Promise<void> | void;
}

type Field =
  | "title"
  | "due_date"
  | "estimation"
  | "location"
  | "link"
  | "label";
type Draft = Record<Field, string>;

const CROSS_OFF_MS = 350;

function toDraft(t: Task): Draft {
  return {
    title: t.title,
    due_date: toDateTimeLocal(t.due_date),
    estimation: t.estimation == null ? "" : String(t.estimation),
    location: t.location ?? "",
    link: t.link ?? "",
    label: t.label ?? "",
  };
}

// <input type="datetime-local"> uses naive local time (no tz). Convert the
// UTC ISO from the API into the local "YYYY-MM-DDTHH:MM" the input expects,
// and back into a UTC ISO when saving.
function toDateTimeLocal(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fromDateTimeLocal(local: string): string | null {
  if (!local) return null;
  return new Date(local).toISOString();
}

export function TaskCard({ task, onChanged }: Props) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [crossing, setCrossing] = useState(false);
  const [draft, setDraft] = useState<Draft>(toDraft(task));
  const labels = useLabels();

  const overdue = isOverdue(task.due_date);
  const urgent = isUrgent(task.due_date, task.estimation);
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

  const save = () =>
    withBusy(async () => {
      await api.updateTask(task.id, {
        title: draft.title,
        due_date: fromDateTimeLocal(draft.due_date),
        estimation: draft.estimation === "" ? null : Number(draft.estimation),
        location: draft.location || null,
        link: draft.link || null,
        label: draft.label || null,
      });
      setEditing(false);
    }, "Saved");

  const TitleEl = task.link ? "a" : "span";
  const titleProps = task.link
    ? { href: task.link, target: "_blank", rel: "noopener noreferrer" }
    : {};

  return (
    <Card
      className={cn(
        "transition-opacity duration-300",
        crossing && "pointer-events-none opacity-40",
      )}
    >
      <CardContent>
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
              <TitleEl
                {...titleProps}
                className={cn(
                  "min-w-0 flex-1 truncate font-medium leading-snug transition-all duration-300",
                  task.link && "hover:underline",
                  crossing && "line-through opacity-60",
                )}
              >
                {task.title}
              </TitleEl>

              <IconButton
                label="Edit task"
                disabled={busy || crossing}
                onClick={() => setEditing((v) => !v)}
                className={cn(
                  "text-muted-foreground hover:text-foreground",
                  editing && "text-foreground",
                )}
              >
                <Settings className="h-4 w-4" />
              </IconButton>
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
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              {task.due_date && (
                <Badge
                  variant={overdue ? "overdue" : urgent ? "urgent" : "open"}
                >
                  {fmtDue(task.due_date)}
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
              {task.location && (
                <span
                  className="inline-flex items-center gap-1"
                  title={task.location}
                >
                  <MapPin className="h-3 w-3" />
                  {task.location.length > 10
                    ? `${String(task.location).charAt(1).toUpperCase() + String(task.location).slice(1, 10)}...`
                    : String(task.location).charAt(0).toUpperCase() +
                      String(task.location).slice(1)}
                </span>
              )}
              {task.estimation != null && (
                <span className="inline-flex items-center gap-1">
                  <Timer className="h-3 w-3" />
                  {task.estimation} min
                </span>
              )}
              {task.ai_doable && <AiDoableDot value={task.ai_doable} />}
            </div>
          </div>
        </div>

        <Collapsible open={editing}>
          <div className="mt-3 space-y-3 border-t pt-3">
            <FieldRow label="Title">
              <Input
                value={draft.title}
                onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              />
            </FieldRow>
            <div className="grid grid-cols-2 gap-4">
              <FieldRow label="Due">
                <Input
                  type="datetime-local"
                  value={draft.due_date}
                  onChange={(e) =>
                    setDraft({ ...draft, due_date: e.target.value })
                  }
                />
              </FieldRow>
              <FieldRow label="Estimation (min)">
                <Input
                  type="number"
                  inputMode="numeric"
                  value={draft.estimation}
                  onChange={(e) =>
                    setDraft({ ...draft, estimation: e.target.value })
                  }
                />
              </FieldRow>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <FieldRow label="Label">
                <LabelSelect
                  value={draft.label}
                  labels={labels}
                  onChange={(v) => setDraft({ ...draft, label: v })}
                />
              </FieldRow>
              <FieldRow label="Location">
                <Input
                  value={draft.location}
                  onChange={(e) =>
                    setDraft({ ...draft, location: e.target.value })
                  }
                />
              </FieldRow>
            </div>
            <FieldRow label="Link">
              <Input
                value={draft.link}
                onChange={(e) => setDraft({ ...draft, link: e.target.value })}
              />
            </FieldRow>
            <div className="flex justify-end gap-2 pt-1">
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => {
                  setDraft(toDraft(task));
                  setEditing(false);
                }}
              >
                Cancel
              </Button>
              <Button size="sm" disabled={busy} onClick={save}>
                Save
              </Button>
            </div>
          </div>
        </Collapsible>
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

function FieldRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      {children}
    </label>
  );
}

function AiDoableDot({ value }: { value: AiDoable }) {
  const color =
    value === "yes"
      ? "bg-emerald-500"
      : value === "no"
        ? "bg-red-500"
        : "bg-amber-400";
  const title =
    value === "yes"
      ? "AI-doable: yes"
      : value === "no"
        ? "AI-doable: no"
        : "AI-doable: unsure";
  return (
    <span
      aria-label={title}
      title={title}
      // 10×10 with a soft outer ring — clearly visible against the muted
      // meta row without dominating the row.
      className={cn(
        "inline-block h-2.5 w-2.5 shrink-0 rounded-full ring-2 ring-background",
        color,
      )}
    />
  );
}

function LabelSelect({
  value,
  labels,
  onChange,
}: {
  value: string;
  labels: Label[];
  onChange: (v: string) => void;
}) {
  const current = labels.find((l) => l.name === value);
  return (
    <div className="flex items-center gap-2">
      <span
        aria-hidden
        className={cn(
          "h-3 w-3 shrink-0 rounded-full",
          labelChipClass(current?.color),
        )}
      />
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        title={current?.description}
        className="flex h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm shadow-xs focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
      >
        <option value="">— none —</option>
        {labels.map((l) => (
          <option key={l.name} value={l.name}>
            {l.name}
          </option>
        ))}
      </select>
    </div>
  );
}
