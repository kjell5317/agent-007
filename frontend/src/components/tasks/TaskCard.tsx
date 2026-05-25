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
import { api } from "@/lib/api";
import { fmtDue, isOverdue } from "@/lib/dates";
import { cn } from "@/lib/utils";
import type { Task } from "@/lib/types";

interface Props {
  task: Task;
  onChanged: () => Promise<void> | void;
}

type Field = "title" | "due_date" | "estimation" | "location" | "link";
type Draft = Record<Field, string>;

const CROSS_OFF_MS = 350;

function toDraft(t: Task): Draft {
  return {
    title: t.title,
    due_date: toDateTimeLocal(t.due_date),
    estimation: t.estimation == null ? "" : String(t.estimation),
    location: t.location ?? "",
    link: t.link ?? "",
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

  const overdue = isOverdue(task.due_date);

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
                <Badge variant={overdue ? "overdue" : "open"}>
                  {fmtDue(task.due_date)}
                </Badge>
              )}
              {task.status === "duplicate" && (
                <Badge variant="duplicate">duplicate</Badge>
              )}
              {task.location && (
                <span className="inline-flex items-center gap-1">
                  <MapPin className="h-3 w-3" />
                  {task.location}
                </span>
              )}
              {task.estimation != null && (
                <span className="inline-flex items-center gap-1">
                  <Timer className="h-3 w-3" />
                  {task.estimation} min
                </span>
              )}
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
            <FieldRow label="Location">
              <Input
                value={draft.location}
                onChange={(e) =>
                  setDraft({ ...draft, location: e.target.value })
                }
              />
            </FieldRow>
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
