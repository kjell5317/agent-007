import { useEffect, useState, type ReactNode } from "react";
import {
  CalendarClock,
  ChevronLeft,
  ExternalLink,
  Github,
  Link2,
  MapPin,
  Pencil,
  RefreshCw,
  Timer,
  X,
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
import { InputBody, MetaDot } from "@/components/inbox/InboxCard";
import { useLabels } from "@/hooks/useLabels";
import { api } from "@/lib/api";
import { fmtDue, fmtWhen, isOverdue, isUrgent } from "@/lib/dates";
import { inboxBadge, inputTitle, senderName } from "@/lib/inbox";
import { labelChipClass } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { Label, Task, TaskRawInput } from "@/lib/types";

interface Props {
  task: Task;
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}

type TextField = "title" | "description" | "link" | "location";
type PickerField = "due_date" | "estimation" | "label";

const TEXT_LABEL: Record<TextField, string> = {
  title: "Title",
  description: "Description",
  link: "Provided link",
  location: "Location",
};

export function TaskDetailModal({ task, onClose, onChanged }: Props) {
  const labels = useLabels();
  const [current, setCurrent] = useState(task);
  const [editingText, setEditingText] = useState<TextField | null>(null);
  const [activePicker, setActivePicker] = useState<PickerField | null>(null);
  const [dateStep, setDateStep] = useState<"date" | "time">("date");
  const [pickerDue, setPickerDue] = useState(task.due_date);
  const [pickerEstimation, setPickerEstimation] = useState(task.estimation);
  const [pickerLabel, setPickerLabel] = useState(task.label ?? "");
  const [textDraft, setTextDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setCurrent(task);
    setEditingText(null);
    setActivePicker(null);
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

  const syncTaskState = (saved: Task) => {
    setCurrent(saved);
    setPickerDue(saved.due_date);
    setPickerEstimation(saved.estimation);
    setPickerLabel(saved.label ?? "");
  };

  async function savePatch(patch: Partial<Task>, message = "Saved") {
    setBusy(true);
    try {
      const saved = await api.updateTask(current.id, patch);
      syncTaskState(saved);
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

  async function runTaskAction(action: () => Promise<Task>, message: string) {
    setBusy(true);
    try {
      const saved = await action();
      syncTaskState(saved);
      setEditingText(null);
      setActivePicker(null);
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

  const rescheduleCurrent = () =>
    runTaskAction(() => api.rescheduleTask(current.id), "Task rescheduled");

  const createGithubIssue = () =>
    runTaskAction(() => api.createGithubIssue(current.id), "GitHub issue created");

  const openTextEditor = (field: TextField) => {
    setActivePicker(null);
    setEditingText(field);
    const value = current[field];
    setTextDraft(value == null ? "" : String(value));
  };

  const closeTextEditor = () => {
    setEditingText(null);
    setTextDraft("");
  };

  const saveTextEditor = async (field: TextField) => {
    const trimmed = textDraft.trim();
    if (field === "title" && !trimmed) {
      toast.error("Title is required");
      return;
    }

    let patch: Partial<Task>;
    if (field === "title") patch = { title: trimmed };
    else if (field === "description") patch = { description: normalizeOptional(textDraft) };
    else if (field === "link") patch = { link: normalizeOptional(textDraft) };
    else patch = { location: normalizeOptional(textDraft) };

    const saved = await savePatch(patch);
    if (saved) closeTextEditor();
  };

  const openPicker = (field: PickerField) => {
    setEditingText(null);
    setActivePicker((prev) => (prev === field ? null : field));
    setDateStep("date");
    setPickerDue(current.due_date);
    setPickerEstimation(current.estimation);
    setPickerLabel(current.label ?? "");
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={
        <TaskTitleHeader
          task={current}
          editing={editingText === "title"}
          draft={textDraft}
          busy={busy}
          onEdit={() => openTextEditor("title")}
          onChange={setTextDraft}
          onCancel={closeTextEditor}
          onSave={() => saveTextEditor("title")}
        />
      }
      titleLabel={current.title}
      titleClassName="text-2xl font-semibold leading-tight"
      className="h-[760px] max-h-[calc(100dvh-2rem)] max-w-3xl"
    >
      {loading ? (
        <div className="min-h-0 flex-1 overflow-hidden">
          <ModalSkeleton />
        </div>
      ) : (
        <TaskSummary
          task={current}
          labels={labels}
          busy={busy}
          editingText={editingText}
          textDraft={textDraft}
          activePicker={activePicker}
          dateStep={dateStep}
          pickerDue={pickerDue}
          pickerEstimation={pickerEstimation}
          pickerLabel={pickerLabel}
          onEditText={openTextEditor}
          onCancelText={closeTextEditor}
          onChangeText={setTextDraft}
          onSaveText={saveTextEditor}
          onEditPicker={openPicker}
          onClosePicker={() => setActivePicker(null)}
          onDateStepChange={setDateStep}
          onPickerDueChange={setPickerDue}
          onPickerEstimationChange={setPickerEstimation}
          onPickerLabelChange={setPickerLabel}
          onSaveDue={async () => {
            const saved = await savePatch({ due_date: pickerDue });
            if (saved) setActivePicker(null);
          }}
          onClearDue={async () => {
            const saved = await savePatch({ due_date: null });
            if (saved) setActivePicker(null);
          }}
          onSaveEstimation={async () => {
            const saved = await savePatch({ estimation: pickerEstimation });
            if (saved) setActivePicker(null);
          }}
          onSaveLabel={async () => {
            const saved = await savePatch({ label: pickerLabel || null });
            if (saved) setActivePicker(null);
          }}
          onReschedule={rescheduleCurrent}
          onCreateGithubIssue={createGithubIssue}
        />
      )}
    </Modal>
  );
}

function TaskTitleHeader({
  task,
  editing,
  draft,
  busy,
  onEdit,
  onChange,
  onCancel,
  onSave,
}: {
  task: Task;
  editing: boolean;
  draft: string;
  busy: boolean;
  onEdit: () => void;
  onChange: (value: string) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  if (editing) {
    return (
      <div className="text-left text-sm font-normal leading-normal">
        <InlineTextEditor
          label={TEXT_LABEL.title}
          value={draft}
          busy={busy}
          onChange={onChange}
          onCancel={onCancel}
          onSave={onSave}
          inputClassName="text-2xl font-semibold leading-tight"
        />
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={onEdit}
      disabled={busy}
      className="group flex w-full min-w-0 items-start justify-center rounded-lg px-2 py-1 text-center text-2xl font-semibold leading-tight transition-colors hover:bg-accent/60 disabled:pointer-events-none disabled:opacity-50"
    >
      <span className="min-w-0 break-words">{task.title}</span>
      <Pencil className="ml-2 mt-1 h-4 w-4 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
  );
}

function TaskSummary({
  task,
  labels,
  busy,
  editingText,
  textDraft,
  activePicker,
  dateStep,
  pickerDue,
  pickerEstimation,
  pickerLabel,
  onEditText,
  onCancelText,
  onChangeText,
  onSaveText,
  onEditPicker,
  onClosePicker,
  onDateStepChange,
  onPickerDueChange,
  onPickerEstimationChange,
  onPickerLabelChange,
  onSaveDue,
  onClearDue,
  onSaveEstimation,
  onSaveLabel,
  onReschedule,
  onCreateGithubIssue,
}: {
  task: Task;
  labels: Label[];
  busy: boolean;
  editingText: TextField | null;
  textDraft: string;
  activePicker: PickerField | null;
  dateStep: "date" | "time";
  pickerDue: string | null;
  pickerEstimation: number | null;
  pickerLabel: string;
  onEditText: (field: TextField) => void;
  onCancelText: () => void;
  onChangeText: (value: string) => void;
  onSaveText: (field: TextField) => void;
  onEditPicker: (field: PickerField) => void;
  onClosePicker: () => void;
  onDateStepChange: (step: "date" | "time") => void;
  onPickerDueChange: (value: string | null) => void;
  onPickerEstimationChange: (value: number | null) => void;
  onPickerLabelChange: (value: string) => void;
  onSaveDue: () => void;
  onClearDue: () => void;
  onSaveEstimation: () => void;
  onSaveLabel: () => void;
  onReschedule: () => void;
  onCreateGithubIssue: () => void;
}) {
  const labelMeta = labels.find((l) => l.name === task.label);
  const dueOverdue = isOverdue(task.due_date);
  const dueUrgent = isUrgent(task.due_date, task.estimation);

  return (
    <div className="min-h-0 flex-1 overflow-auto pr-1">
      <div className="space-y-5">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <PickerAnchor
              open={activePicker === "label"}
              panel={
                <InlinePickerPanel title="Label" onClose={onClosePicker}>
                  <LabelPicker
                    value={pickerLabel}
                    onChange={onPickerLabelChange}
                    onSave={onSaveLabel}
                    labels={labels}
                  />
                </InlinePickerPanel>
              }
            >
              <button
                type="button"
                onClick={() => onEditPicker("label")}
                disabled={busy}
                className="rounded-full transition-transform hover:scale-[1.02] disabled:pointer-events-none disabled:opacity-50"
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
            </PickerAnchor>

            {task.scheduled_date && (
              <Badge variant="muted" className="gap-1">
                <CalendarClock className="h-3 w-3" />
                Scheduled {fmtDue(task.scheduled_date)}
              </Badge>
            )}

            <PickerAnchor
              open={activePicker === "estimation"}
              panel={
                <InlinePickerPanel title="Estimate" onClose={onClosePicker}>
                  <EstimationPicker
                    value={pickerEstimation}
                    onChange={onPickerEstimationChange}
                    onSave={onSaveEstimation}
                  />
                </InlinePickerPanel>
              }
            >
              <button
                type="button"
                onClick={() => onEditPicker("estimation")}
                disabled={busy}
                className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 font-medium transition-colors hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
              >
                <Timer className="h-3 w-3" />
                {task.estimation != null ? `${task.estimation} min` : "No estimate"}
              </button>
            </PickerAnchor>

            <PickerAnchor
              open={activePicker === "due_date"}
              panel={
                <InlinePickerPanel
                  title="Due date"
                  onClose={onClosePicker}
                  onBack={dateStep === "time" ? () => onDateStepChange("date") : undefined}
                  footer={
                    pickerDue ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="w-full"
                        onClick={onClearDue}
                        disabled={busy}
                      >
                        Clear due date
                      </Button>
                    ) : null
                  }
                >
                  <DatePicker
                    value={pickerDue}
                    onChange={onPickerDueChange}
                    onSave={onSaveDue}
                    step={dateStep}
                    onStepChange={onDateStepChange}
                  />
                </InlinePickerPanel>
              }
            >
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
            </PickerAnchor>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onReschedule}
              disabled={busy}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              Reschedule
            </Button>
          </div>
        </div>

        <div className="space-y-1.5">
          <EditableTextBlock
            field="location"
            icon={<MapPin className="h-4 w-4" />}
            value={task.location}
            fallback="Add location"
            editing={editingText === "location"}
            draft={textDraft}
            busy={busy}
            onEdit={() => onEditText("location")}
            onChange={onChangeText}
            onCancel={onCancelText}
            onSave={() => onSaveText("location")}
          />
          <LinksSection
            task={task}
            editing={editingText === "link"}
            draft={textDraft}
            busy={busy}
            onEdit={() => onEditText("link")}
            onChange={onChangeText}
            onCancel={onCancelText}
            onSave={() => onSaveText("link")}
            onCreateGithubIssue={onCreateGithubIssue}
          />
        </div>

        <EditableTextBlock
          field="description"
          value={task.description}
          fallback="Add description"
          editing={editingText === "description"}
          draft={textDraft}
          busy={busy}
          multiline
          onEdit={() => onEditText("description")}
          onChange={onChangeText}
          onCancel={onCancelText}
          onSave={() => onSaveText("description")}
        />

        <LinkedInputsSection inputs={task.raw_inputs ?? []} />
      </div>
    </div>
  );
}

function EditableTextBlock({
  field,
  icon,
  value,
  fallback,
  editing,
  draft,
  busy,
  multiline,
  onEdit,
  onChange,
  onCancel,
  onSave,
}: {
  field: TextField;
  icon?: ReactNode;
  value: string | null;
  fallback: string;
  editing: boolean;
  draft: string;
  busy: boolean;
  multiline?: boolean;
  onEdit: () => void;
  onChange: (value: string) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  if (editing) {
    return (
      <div className="rounded-lg bg-accent/40 p-2">
        <InlineTextEditor
          label={TEXT_LABEL[field]}
          value={draft}
          busy={busy}
          multiline={multiline}
          placeholder={fallback}
          onChange={onChange}
          onCancel={onCancel}
          onSave={onSave}
        />
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={onEdit}
      disabled={busy}
      className="group flex w-full min-w-0 items-start gap-3 rounded-lg p-2 text-left transition-colors hover:bg-accent/60 disabled:pointer-events-none disabled:opacity-50"
    >
      {icon && <span className="mt-0.5 shrink-0 text-muted-foreground">{icon}</span>}
      <span className="min-w-0 flex-1">
        <span className="block text-xs font-medium uppercase text-muted-foreground">
          {TEXT_LABEL[field]}
        </span>
        {value ? (
          <span
            className={cn(
              "block break-words text-sm",
              multiline && "whitespace-pre-wrap leading-relaxed",
            )}
          >
            {value}
          </span>
        ) : (
          <span className="block text-sm text-muted-foreground">{fallback}</span>
        )}
      </span>
      <Pencil className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
  );
}

function LinksSection({
  task,
  editing,
  draft,
  busy,
  onEdit,
  onChange,
  onCancel,
  onSave,
  onCreateGithubIssue,
}: {
  task: Task;
  editing: boolean;
  draft: string;
  busy: boolean;
  onEdit: () => void;
  onChange: (value: string) => void;
  onCancel: () => void;
  onSave: () => void;
  onCreateGithubIssue: () => void;
}) {
  if (editing) {
    return (
      <div className="rounded-lg bg-accent/40 p-2">
        <InlineTextEditor
          label={TEXT_LABEL.link}
          value={draft}
          busy={busy}
          placeholder="https://..."
          onChange={onChange}
          onCancel={onCancel}
          onSave={onSave}
        />
      </div>
    );
  }

  return (
    <div className="space-y-1">
      <button
        type="button"
        onClick={onEdit}
        disabled={busy}
        className="group flex w-full min-w-0 items-start gap-3 rounded-lg p-2 text-left transition-colors hover:bg-accent/60 disabled:pointer-events-none disabled:opacity-50"
      >
        <span className="mt-0.5 shrink-0 text-muted-foreground">
          <Link2 className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-xs font-medium uppercase text-muted-foreground">
            {TEXT_LABEL.link}
          </span>
          <span
            className={cn(
              "block break-words text-sm",
              !task.link && "text-muted-foreground",
            )}
          >
            {task.link || "Add provided link"}
          </span>
        </span>
        <Pencil className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
      </button>
      <div className="flex flex-wrap gap-1 pl-9">
        {task.link && <OpenLink href={task.link}>Open provided link</OpenLink>}
        {canCreateGithubIssue(task) && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={onCreateGithubIssue}
            disabled={busy}
            className="h-8 px-2 text-primary"
          >
            <Github className="h-3.5 w-3.5" />
            Create GitHub issue
          </Button>
        )}
      </div>
    </div>
  );
}

function InlineTextEditor({
  label,
  value,
  busy,
  multiline,
  placeholder,
  inputClassName,
  onChange,
  onCancel,
  onSave,
}: {
  label: string;
  value: string;
  busy: boolean;
  multiline?: boolean;
  placeholder?: string;
  inputClassName?: string;
  onChange: (value: string) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  return (
    <div className="space-y-2">
      <label className="space-y-1.5">
        <span className="text-xs font-medium uppercase text-muted-foreground">
          {label}
        </span>
        {multiline ? (
          <Textarea
            value={value}
            onChange={(e) => onChange(e.target.value)}
            disabled={busy}
            placeholder={placeholder}
            className="min-h-32 resize-y"
            autoFocus
          />
        ) : (
          <Input
            value={value}
            onChange={(e) => onChange(e.target.value)}
            disabled={busy}
            placeholder={placeholder}
            className={inputClassName}
            autoFocus
          />
        )}
      </label>
      <div className="flex justify-end gap-2">
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

function PickerAnchor({
  open,
  panel,
  children,
}: {
  open: boolean;
  panel: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="relative inline-flex">
      {children}
      {open && panel}
    </div>
  );
}

function InlinePickerPanel({
  title,
  children,
  footer,
  onBack,
  onClose,
}: {
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  onBack?: () => void;
  onClose: () => void;
}) {
  return (
    <div
      className="absolute left-0 top-full z-20 mt-2 w-[min(calc(100vw-4rem),22rem)] rounded-lg border bg-card p-3 text-card-foreground shadow-lg"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="mb-2 grid grid-cols-[1.75rem_1fr_1.75rem] items-center">
        <div>
          {onBack && (
            <button
              type="button"
              aria-label="Back"
              onClick={onBack}
              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
          )}
        </div>
        <div className="truncate text-center text-sm font-semibold">{title}</div>
        <button
          type="button"
          aria-label="Close picker"
          onClick={onClose}
          className="inline-flex h-7 w-7 items-center justify-center justify-self-end rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="space-y-3">
        {children}
        {footer}
      </div>
    </div>
  );
}

function LinkedInputsSection({ inputs }: { inputs: TaskRawInput[] }) {
  if (inputs.length === 0) return null;

  return (
    <section className="space-y-2 border-t pt-3">
      <div className="text-xs font-medium uppercase text-muted-foreground">
        Linked inputs
      </div>
      <div className="space-y-2">
        {inputs.map((input) => (
          <div key={input.id} className="rounded-lg border p-3">
            <div className="flex min-w-0 items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">{inputTitle(input)}</div>
                <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                  <Badge variant={inboxBadge(input)}>{inboxBadge(input)}</Badge>
                  <span className="truncate font-medium">{senderName(input)}</span>
                  <MetaDot />
                  <span className="font-medium">{fmtWhen(input.received_at)}</span>
                </div>
              </div>
              {input.source_url && (
                <OpenLink href={input.source_url}>Open source</OpenLink>
              )}
            </div>
            <div className="mt-3 space-y-3 border-t pt-3 text-sm">
              <InputBody data={input} />
            </div>
          </div>
        ))}
      </div>
    </section>
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

function normalizeOptional(value: string) {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function canCreateGithubIssue(task: Task) {
  return (task.label === "CSEE" || task.label === "Social AI") && !hasGithubUrl(task.link);
}

function hasGithubUrl(value: string | null) {
  if (!value) return false;
  return /^(https?:\/\/)?(www\.)?github\.com(\/|$)/i.test(value.trim());
}
