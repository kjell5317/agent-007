import { useEffect, useState, type ReactNode } from "react";
import {
  CalendarClock,
  ChevronLeft,
  ExternalLink,
  Link2,
  MapPin,
  Pencil,
  Timer,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DatePicker } from "@/components/ui/date-picker";
import { EstimationPicker } from "@/components/ui/estimation-picker";
import { Input } from "@/components/ui/input";
import { LabelPicker } from "@/components/ui/label-picker";
import { Modal } from "@/components/ui/modal";
import { ModalSkeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useLabels } from "@/hooks/useLabels";
import { api } from "@/lib/api";
import { fmtDue, fmtWhen, isOverdue, isUrgent } from "@/lib/dates";
import { labelChipClass } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { Task } from "@/lib/types";

interface Props {
  task: Task;
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}

type Mode =
  | "summary"
  | "title"
  | "description"
  | "link"
  | "location"
  | "due_date"
  | "estimation"
  | "label";

type TextMode = Extract<Mode, "title" | "description" | "link" | "location">;

const TEXT_LABEL: Record<TextMode, string> = {
  title: "Title",
  description: "Description",
  link: "Provided link",
  location: "Location",
};

export function TaskDetailModal({ task, onClose, onChanged }: Props) {
  const labels = useLabels();
  const [current, setCurrent] = useState(task);
  const [mode, setMode] = useState<Mode>("summary");
  const [dateStep, setDateStep] = useState<"date" | "time">("date");
  const [pickerDue, setPickerDue] = useState(task.due_date);
  const [pickerEstimation, setPickerEstimation] = useState(task.estimation);
  const [pickerLabel, setPickerLabel] = useState(task.label ?? "");
  const [textDraft, setTextDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setCurrent(task);
    setMode("summary");
    setDateStep("date");
    setPickerDue(task.due_date);
    setPickerEstimation(task.estimation);
    setPickerLabel(task.label ?? "");
  }, [task]);

  useEffect(() => {
    if (!loading) return;
    const timer = window.setTimeout(() => setLoading(false), 120);
    return () => window.clearTimeout(timer);
  }, [loading]);

  async function savePatch(patch: Partial<Task>, message = "Saved") {
    setBusy(true);
    try {
      const saved = await api.updateTask(current.id, patch);
      setCurrent(saved);
      setPickerDue(saved.due_date);
      setPickerEstimation(saved.estimation);
      setPickerLabel(saved.label ?? "");
      toast.success(message);
      await onChanged();
      return saved;
    } catch (e) {
      toast.error((e as Error).message);
      return null;
    } finally {
      setBusy(false);
    }
  }

  const openTextEditor = (nextMode: TextMode) => {
    const value = current[nextMode];
    setTextDraft(value == null ? "" : String(value));
    setMode(nextMode);
  };

  const saveTextEditor = async (textMode: TextMode) => {
    const trimmed = textDraft.trim();
    if (textMode === "title" && !trimmed) {
      toast.error("Title is required");
      return;
    }
    const saved = await savePatch({
      [textMode]: textMode === "title" ? trimmed : normalizeOptional(textDraft),
    });
    if (saved) setMode("summary");
  };

  const openPicker = (next: Extract<Mode, "due_date" | "estimation" | "label">) => {
    setMode(next);
    setDateStep("date");
    setPickerDue(current.due_date);
    setPickerEstimation(current.estimation);
    setPickerLabel(current.label ?? "");
  };

  const closeSubView = () => {
    setMode("summary");
    setDateStep("date");
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={mode === "summary" ? "Task details" : editTitle(mode)}
      titleClassName="text-lg"
      className="h-[760px] max-h-[calc(100dvh-2rem)] max-w-3xl"
      leftAction={
        mode !== "summary" ? (
          <button
            type="button"
            aria-label="Back"
            onClick={() =>
              mode === "due_date" && dateStep === "time"
                ? setDateStep("date")
                : closeSubView()
            }
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
        ) : null
      }
    >
      {loading ? (
        <div className="min-h-0 flex-1 overflow-hidden">
          <ModalSkeleton />
        </div>
      ) : mode === "summary" ? (
        <TaskSummary
          task={current}
          labels={labels}
          busy={busy}
          onEditText={openTextEditor}
          onEditPicker={openPicker}
        />
      ) : isTextMode(mode) ? (
        <TextEditor
          mode={mode}
          value={textDraft}
          busy={busy}
          onChange={setTextDraft}
          onCancel={closeSubView}
          onSave={() => saveTextEditor(mode)}
        />
      ) : mode === "due_date" ? (
        <PickerBody
          footer={
            pickerDue ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="w-full"
                onClick={async () => {
                  const saved = await savePatch({ due_date: null });
                  if (saved) closeSubView();
                }}
                disabled={busy}
              >
                Clear due date
              </Button>
            ) : null
          }
        >
          <DatePicker
            value={pickerDue}
            onChange={setPickerDue}
            onSave={async () => {
              const saved = await savePatch({ due_date: pickerDue });
              if (saved) closeSubView();
            }}
            step={dateStep}
            onStepChange={setDateStep}
          />
        </PickerBody>
      ) : mode === "estimation" ? (
        <PickerBody>
          <EstimationPicker
            value={pickerEstimation}
            onChange={setPickerEstimation}
            onSave={async () => {
              const saved = await savePatch({ estimation: pickerEstimation });
              if (saved) closeSubView();
            }}
          />
        </PickerBody>
      ) : (
        <PickerBody>
          <LabelPicker
            value={pickerLabel}
            onChange={setPickerLabel}
            onSave={async () => {
              const saved = await savePatch({ label: pickerLabel || null });
              if (saved) closeSubView();
            }}
            labels={labels}
          />
        </PickerBody>
      )}
    </Modal>
  );
}

function TaskSummary({
  task,
  labels,
  busy,
  onEditText,
  onEditPicker,
}: {
  task: Task;
  labels: ReturnType<typeof useLabels>;
  busy: boolean;
  onEditText: (mode: TextMode) => void;
  onEditPicker: (mode: Extract<Mode, "due_date" | "estimation" | "label">) => void;
}) {
  const labelMeta = labels.find((l) => l.name === task.label);
  const dueOverdue = isOverdue(task.due_date);
  const dueUrgent = isUrgent(task.due_date, task.estimation);

  return (
    <div className="min-h-0 flex-1 overflow-auto pr-1">
      <div className="space-y-5">
        <div className="space-y-2">
          <div className="flex min-w-0 flex-wrap items-start gap-2">
            <button
              type="button"
              onClick={() => onEditText("title")}
              disabled={busy}
              className="group min-w-0 flex-1 rounded-md text-left text-2xl font-semibold leading-tight transition-colors hover:text-primary disabled:pointer-events-none disabled:opacity-50"
            >
              <span className="break-words">{task.title}</span>
              <Pencil className="ml-2 inline h-4 w-4 align-baseline text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
            </button>
            <button
              type="button"
              onClick={() => onEditPicker("label")}
              disabled={busy}
              className="shrink-0 rounded-full transition-transform hover:scale-[1.02] disabled:pointer-events-none disabled:opacity-50"
              title={labelMeta?.description ?? task.label ?? "Set label"}
            >
              {task.label ? (
                <span
                  className={cn(
                    "inline-flex items-center rounded-full px-2.5 py-1 text-xs font-medium",
                    labelChipClass(labelMeta?.color),
                  )}
                >
                  {task.label}
                </span>
              ) : (
                <Badge variant="muted">No label</Badge>
              )}
            </button>
          </div>

          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {task.scheduled_date && (
              <Badge variant="muted" className="gap-1">
                <CalendarClock className="h-3 w-3" />
                Scheduled {fmtDue(task.scheduled_date)}
              </Badge>
            )}
            <button
              type="button"
              onClick={() => onEditPicker("estimation")}
              disabled={busy}
              className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 font-medium transition-colors hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
            >
              <Timer className="h-3 w-3" />
              {task.estimation != null ? `${task.estimation} min` : "No estimate"}
            </button>
            <button
              type="button"
              onClick={() => onEditPicker("due_date")}
              disabled={busy}
              className="disabled:pointer-events-none disabled:opacity-50"
            >
              {task.due_date ? (
                <Badge variant={dueOverdue ? "overdue" : dueUrgent ? "urgent" : "open"}>
                  Due {fmtDue(task.due_date)}
                </Badge>
              ) : (
                <Badge variant="muted">No due date</Badge>
              )}
            </button>
          </div>
        </div>

        <div className="space-y-1.5">
          <SummaryRow
            icon={<MapPin className="h-4 w-4" />}
            label="Location"
            value={task.location || "Add location"}
            muted={!task.location}
            disabled={busy}
            onClick={() => onEditText("location")}
          />
          <LinksSection
            task={task}
            busy={busy}
            onEditProvided={() => onEditText("link")}
          />
        </div>

        <button
          type="button"
          onClick={() => onEditText("description")}
          disabled={busy}
          className="group block w-full rounded-lg p-2 text-left transition-colors hover:bg-accent/60 disabled:pointer-events-none disabled:opacity-50"
        >
          <div className="mb-1 flex items-center justify-between gap-2 text-xs font-medium uppercase text-muted-foreground">
            <span>Description</span>
            <Pencil className="h-3.5 w-3.5 opacity-0 transition-opacity group-hover:opacity-100" />
          </div>
          {task.description ? (
            <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">
              {task.description}
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">Add description</p>
          )}
        </button>

        <div className="grid gap-2 border-t pt-3 text-xs text-muted-foreground sm:grid-cols-2">
          <Meta label="Status">
            <Badge variant={task.status}>{task.status}</Badge>
          </Meta>
          <Meta label="Source">{task.is_manual ? "Manual" : "Extracted"}</Meta>
          <Meta label="Created">{fmtWhen(task.created_at)}</Meta>
          <Meta label="Updated">{fmtWhen(task.updated_at)}</Meta>
        </div>
      </div>
    </div>
  );
}

function LinksSection({
  task,
  busy,
  onEditProvided,
}: {
  task: Task;
  busy: boolean;
  onEditProvided: () => void;
}) {
  const hasSource = Boolean(task.source_url && task.source_url !== task.link);

  return (
    <div className="rounded-lg p-2">
      <div className="mb-1 flex items-center gap-2 text-xs font-medium uppercase text-muted-foreground">
        <Link2 className="h-3.5 w-3.5" />
        <span>Links</span>
      </div>
      <div className="space-y-1">
        <button
          type="button"
          onClick={onEditProvided}
          disabled={busy}
          className="group flex w-full min-w-0 items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-50"
        >
          <span className={cn("min-w-0 truncate", !task.link && "text-muted-foreground")}>
            {task.link || "Add provided link"}
          </span>
          <Pencil className="h-3.5 w-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
        </button>
        {task.link && <OpenLink href={task.link}>Open provided link</OpenLink>}
        {hasSource && task.source_url && (
          <OpenLink href={task.source_url}>Open original source</OpenLink>
        )}
      </div>
    </div>
  );
}

function SummaryRow({
  icon,
  label,
  value,
  muted,
  disabled,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  muted?: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="group flex w-full min-w-0 items-start gap-3 rounded-lg p-2 text-left transition-colors hover:bg-accent/60 disabled:pointer-events-none disabled:opacity-50"
    >
      <span className="mt-0.5 shrink-0 text-muted-foreground">{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block text-xs font-medium uppercase text-muted-foreground">
          {label}
        </span>
        <span className={cn("block break-words text-sm", muted && "text-muted-foreground")}>
          {value}
        </span>
      </span>
      <Pencil className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
  );
}

function OpenLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex max-w-full items-center gap-1 rounded-md px-2 py-1 text-sm font-medium text-primary hover:bg-accent hover:underline"
    >
      <ExternalLink className="h-3.5 w-3.5 shrink-0" />
      <span className="min-w-0 truncate">{children}</span>
    </a>
  );
}

function TextEditor({
  mode,
  value,
  busy,
  onChange,
  onCancel,
  onSave,
}: {
  mode: TextMode;
  value: string;
  busy: boolean;
  onChange: (value: string) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  const isLong = mode === "description";
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <label className="min-h-0 flex-1 space-y-1.5">
        <span className="text-xs font-medium text-muted-foreground">
          {TEXT_LABEL[mode]}
        </span>
        {isLong ? (
          <Textarea
            value={value}
            onChange={(e) => onChange(e.target.value)}
            disabled={busy}
            className="h-[calc(100%-1.5rem)] min-h-[24rem] resize-none"
            autoFocus
          />
        ) : (
          <Input
            value={value}
            onChange={(e) => onChange(e.target.value)}
            disabled={busy}
            placeholder={mode === "link" ? "https://..." : undefined}
            autoFocus
          />
        )}
      </label>
      <div className="flex shrink-0 justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button type="button" size="sm" onClick={onSave} disabled={busy}>
          Save
        </Button>
      </div>
    </div>
  );
}

function PickerBody({
  children,
  footer,
}: {
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className="min-h-0 flex-1 overflow-auto">
      <div className="mx-auto w-full max-w-sm space-y-3">{children}{footer}</div>
    </div>
  );
}

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="font-medium">{label}</div>
      <div className="mt-1 min-w-0 break-words text-foreground">{children}</div>
    </div>
  );
}

function editTitle(mode: Mode) {
  if (mode === "due_date") return "Edit due date";
  if (mode === "estimation") return "Edit estimation";
  if (mode === "label") return "Edit label";
  if (isTextMode(mode)) return `Edit ${TEXT_LABEL[mode].toLowerCase()}`;
  return "Task details";
}

function isTextMode(mode: Mode): mode is TextMode {
  return mode === "title" || mode === "description" || mode === "link" || mode === "location";
}

function normalizeOptional(value: string) {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}
