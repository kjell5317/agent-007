import { useState } from "react";
import { MapPin, Timer } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { fmtDue, isOverdue } from "@/lib/dates";
import type { Task } from "@/lib/types";

interface Props {
  task: Task;
  onChanged: () => Promise<void> | void;
}

type Field = "title" | "description" | "due_date" | "estimation" | "location" | "link";
type Draft = Record<Field, string>;

function toDraft(t: Task): Draft {
  return {
    title: t.title,
    description: t.description ?? "",
    due_date: t.due_date ?? "",
    estimation: t.estimation == null ? "" : String(t.estimation),
    location: t.location ?? "",
    link: t.link ?? "",
  };
}

export function TaskCard({ task, onChanged }: Props) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
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

  const save = () =>
    withBusy(async () => {
      const patch: Partial<Task> = {
        title: draft.title,
        description: draft.description || null,
        due_date: draft.due_date || null,
        estimation: draft.estimation === "" ? null : Number(draft.estimation),
        location: draft.location || null,
        link: draft.link || null,
      };
      await api.updateTask(task.id, patch);
    }, "Saved");

  return (
    <Card>
      <CardContent
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button,input,textarea,a")) return;
          setOpen((v) => !v);
        }}
        className="cursor-pointer"
      >
        <div className="font-medium leading-snug">{task.title}</div>
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          {task.due_date && (
            <Badge variant={overdue ? "overdue" : "open"}>
              {overdue ? "overdue · " : ""}
              {fmtDue(task.due_date)}
            </Badge>
          )}
          {task.status === "duplicate" && <Badge variant="duplicate">duplicate</Badge>}
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

        <Collapsible open={open}>
          <div className="mt-3 space-y-3 border-t pt-3" onClick={(e) => e.stopPropagation()}>
            <Field label="Title">
              <Input
                value={draft.title}
                onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              />
            </Field>
            <Field label="Description">
              <Textarea
                value={draft.description}
                onChange={(e) => setDraft({ ...draft, description: e.target.value })}
              />
            </Field>
            <Field label="Due date (ISO)">
              <Input
                value={draft.due_date}
                placeholder="2026-05-30T10:00:00Z"
                onChange={(e) => setDraft({ ...draft, due_date: e.target.value })}
              />
            </Field>
            <Field label="Estimation (min)">
              <Input
                type="number"
                inputMode="numeric"
                value={draft.estimation}
                onChange={(e) => setDraft({ ...draft, estimation: e.target.value })}
              />
            </Field>
            <Field label="Location">
              <Input
                value={draft.location}
                onChange={(e) => setDraft({ ...draft, location: e.target.value })}
              />
            </Field>
            <Field label="Link">
              <Input
                value={draft.link}
                onChange={(e) => setDraft({ ...draft, link: e.target.value })}
              />
            </Field>
            <div className="flex flex-wrap gap-2 pt-1">
              <Button
                size="sm"
                disabled={busy}
                onClick={() => withBusy(() => api.closeTask(task.id), "Marked done")}
              >
                Done
              </Button>
              <Button size="sm" variant="outline" disabled={busy} onClick={save}>
                Save
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="text-destructive"
                disabled={busy}
                onClick={() => withBusy(() => api.markNotTask(task.id), "Marked not a task")}
              >
                Not a task
              </Button>
            </div>
          </div>
        </Collapsible>
      </CardContent>
    </Card>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      {children}
    </label>
  );
}
