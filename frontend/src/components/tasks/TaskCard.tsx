import { useState } from "react";
import {
  ChevronLeft,
  Circle,
  CircleCheckBig,
  MapPin,
  Timer,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { DatePicker } from "@/components/ui/date-picker";
import { EstimationPicker } from "@/components/ui/estimation-picker";
import { LabelPicker } from "@/components/ui/label-picker";
import { Modal } from "@/components/ui/modal";
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

type EditField = "due_date" | "estimation" | "label" | null;

const CROSS_OFF_MS = 350;

export function TaskCard({ task, onChanged, seenAfter }: Props) {
  const [busy, setBusy] = useState(false);
  const [crossing, setCrossing] = useState(false);
  const [editField, setEditField] = useState<EditField>(null);
  const [draft, setDraft] = useState("");
  // Lifted out of DatePicker so the surrounding Modal can render a Back
  // arrow into its top-left action slot on the time step.
  const [dateStep, setDateStep] = useState<"date" | "time">("date");
  const labels = useLabels();

  const overdue = isOverdue(task.due_date);
  const urgent = isUrgent(task.due_date, task.estimation);
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

  const openEdit = (field: Exclude<EditField, null>) => {
    if (field === "due_date") setDraft(task.due_date ?? "");
    else if (field === "estimation")
      setDraft(task.estimation == null ? "" : String(task.estimation));
    else setDraft(task.label ?? "");
    setDateStep("date");
    setEditField(field);
  };

  const closeEdit = () => setEditField(null);

  const saveEdit = async () => {
    if (!editField) return;
    let patch: Partial<Task>;
    if (editField === "due_date") {
      patch = { due_date: draft || null };
    } else if (editField === "estimation") {
      patch = { estimation: draft === "" ? null : Number(draft) };
    } else {
      patch = { label: draft || null };
    }
    await withBusy(() => api.updateTask(task.id, patch), "Saved");
    closeEdit();
  };

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
              {unread && (
                <span
                  aria-label="Unread"
                  title="Unread"
                  className="inline-block h-2 w-2 shrink-0 rounded-full bg-emerald-500"
                />
              )}
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
              {task.due_date ? (
                <button
                  type="button"
                  onClick={() => openEdit("due_date")}
                  disabled={busy || crossing}
                  className="rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <Badge
                    variant={overdue ? "overdue" : urgent ? "urgent" : "open"}
                  >
                    {fmtDue(task.due_date)}
                  </Badge>
                </button>
              ) : (
                <AddChip
                  label="+ Due"
                  onClick={() => openEdit("due_date")}
                  disabled={busy || crossing}
                />
              )}

              {task.label ? (
                <button
                  type="button"
                  onClick={() => openEdit("label")}
                  disabled={busy || crossing}
                  title={labelMeta?.description ?? task.label}
                  className={cn(
                    "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    labelChipClass(labelMeta?.color),
                  )}
                >
                  {task.label}
                </button>
              ) : (
                <AddChip
                  label="+ Label"
                  onClick={() => openEdit("label")}
                  disabled={busy || crossing}
                />
              )}

              {task.estimation != null ? (
                <button
                  type="button"
                  onClick={() => openEdit("estimation")}
                  disabled={busy || crossing}
                  className="inline-flex items-center gap-1 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <Timer className="h-3 w-3" />
                  {task.estimation} min
                </button>
              ) : (
                <AddChip
                  label="+ Estimation"
                  onClick={() => openEdit("estimation")}
                  disabled={busy || crossing}
                />
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
        </div>
      </CardContent>

      <Modal
        open={editField !== null}
        onClose={closeEdit}
        title={
          editField === "due_date"
            ? "Edit due date"
            : editField === "estimation"
              ? "Edit estimation"
              : editField === "label"
                ? "Edit label"
                : ""
        }
        leftAction={
          editField === "due_date" && dateStep === "time" ? (
            <button
              type="button"
              aria-label="Back"
              onClick={() => setDateStep("date")}
              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
          ) : null
        }
      >
        {editField === "due_date" && (
          // DatePicker owns its own Next / Save footer; the modal's
          // leftAction slot supplies the Back arrow on the time step.
          <DatePicker
            value={draft || null}
            onChange={(iso) => setDraft(iso ?? "")}
            onSave={saveEdit}
            step={dateStep}
            onStepChange={setDateStep}
          />
        )}
        {editField === "estimation" && (
          <EstimationPicker
            value={draft === "" ? null : Number(draft)}
            onChange={(n) => setDraft(n == null ? "" : String(n))}
            onSave={saveEdit}
          />
        )}
        {editField === "label" && (
          <LabelPicker
            value={draft}
            onChange={(v) => setDraft(v)}
            onSave={saveEdit}
            labels={labels}
          />
        )}
      </Modal>
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

function AddChip({
  label,
  onClick,
  disabled,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-md px-1 text-[11px] text-muted-foreground/70 hover:text-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {label}
    </button>
  );
}

