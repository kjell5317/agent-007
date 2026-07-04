import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  AlarmClock,
  CalendarClock,
  ChevronLeft,
  Circle,
  CircleCheckBig,
  ExternalLink,
  Github,
  Link2,
  MapPin,
  Pencil,
  RefreshCw,
  RotateCcw,
  Timer,
  Trash2,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { DatePicker } from "@/components/ui/date-picker";
import { EstimationPicker } from "@/components/ui/estimation-picker";
import { Input } from "@/components/ui/input";
import { LabelPicker } from "@/components/ui/label-picker";
import { Modal } from "@/components/ui/modal";
import { ModalSkeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { hasInputDetails, InputBody, MetaDot } from "@/components/inbox/InboxCard";
import { KotxRunSection } from "@/components/tasks/KotxRunSection";
import { InputStatusBadge, RunStatusBadge } from "@/components/runs/RunStatusBadge";
import { useLabels } from "@/hooks/useLabels";
import { api } from "@/lib/api";
import { fmtDue, fmtWhen, isOverdue, isUrgent } from "@/lib/dates";
import { inputTitle, senderName } from "@/lib/inbox";
import type { KotxTask } from "@/lib/kotx";
import { labelChipClass } from "@/lib/labels";
import { pollTaskCreation, type PollHandle } from "@/lib/pollTask";
import { cn } from "@/lib/utils";
import type { Label, Task, TaskRawInput } from "@/lib/types";

interface Props {
  task: Task;
  kotxTask?: KotxTask | null;
  onClose: () => void;
  onChanged: () => Promise<void> | void;
  onKotxChanged?: () => Promise<void> | void;
}

type TextField = "title" | "description" | "link" | "location";
type PickerField = "due_date" | "estimation" | "label";

const TEXT_LABEL: Record<TextField, string> = {
  title: "Title",
  description: "Description",
  link: "Provided link",
  location: "Location",
};

const TASK_SUMMARY_BADGE_BUTTON_CLASS =
  "relative inline-flex h-8 items-center justify-center overflow-hidden rounded-full text-xs font-medium transition-colors before:pointer-events-none before:absolute before:inset-0 before:z-10 before:rounded-full before:bg-foreground/0 before:content-[''] before:transition-colors hover:before:bg-foreground/[0.06] disabled:pointer-events-none disabled:opacity-50 dark:hover:before:bg-white/[0.08]";
const TASK_SUMMARY_BADGE_CONTENT_CLASS =
  "relative z-20 inline-flex h-full items-center gap-1 rounded-full border border-transparent px-3";
const TASK_SUMMARY_MUTED_BADGE_CLASS = "bg-muted text-muted-foreground";
const TASK_SUMMARY_OPEN_BADGE_CLASS =
  "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/20 dark:text-emerald-200";
const TASK_SUMMARY_URGENT_BADGE_CLASS =
  "bg-orange-500 text-white dark:bg-orange-500/25 dark:text-orange-100";
const TASK_SUMMARY_OVERDUE_BADGE_CLASS =
  "bg-red-500 text-white dark:bg-red-500/25 dark:text-red-100";
const TASK_SUMMARY_SCHEDULED_BADGE_CLASS =
  "bg-sky-100 text-sky-800 dark:bg-sky-500/20 dark:text-sky-200";

export function TaskDetailModal({
  task,
  kotxTask = null,
  onClose,
  onChanged,
  onKotxChanged,
}: Props) {
  const labels = useLabels();
  const [current, setCurrent] = useState(task);
  const [editingText, setEditingText] = useState<TextField | null>(null);
  const [activePicker, setActivePicker] = useState<PickerField | null>(null);
  const [dateStep, setDateStep] = useState<"date" | "time">("date");
  const [pickerDue, setPickerDue] = useState(task.due_date);
  const [pickerEstimation, setPickerEstimation] = useState(task.estimation);
  const [pickerLabel, setPickerLabel] = useState(task.label ?? "");
  const [textDraft, setTextDraft] = useState("");
  const [locationSuggestions, setLocationSuggestions] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [closingAction, setClosingAction] = useState<"done" | "dismiss" | null>(
    null,
  );
  const locationSuggestionRequestRef = useRef(0);
  const activeReopenPoll = useRef<PollHandle | null>(null);

  useEffect(() => {
    setCurrent(task);
    setEditingText(null);
    setActivePicker(null);
    setDateStep("date");
    setPickerDue(task.due_date);
    setPickerEstimation(task.estimation);
    setPickerLabel(task.label ?? "");
    setClosingAction(null);
  }, [task]);

  useEffect(() => {
    if (!loading) return;
    const timer = window.setTimeout(() => setLoading(false), 120);
    return () => window.clearTimeout(timer);
  }, [loading]);

  useEffect(
    () => () => {
      activeReopenPoll.current?.cancel();
      activeReopenPoll.current = null;
    },
    [],
  );

  useEffect(() => {
    if (editingText !== "location") {
      locationSuggestionRequestRef.current += 1;
      setLocationSuggestions([]);
      return;
    }

    const requestId = locationSuggestionRequestRef.current + 1;
    locationSuggestionRequestRef.current = requestId;
    let cancelled = false;

    api
      .locationSuggestions(textDraft)
      .then(({ suggestions }) => {
        if (!cancelled && locationSuggestionRequestRef.current === requestId) {
          setLocationSuggestions(suggestions);
        }
      })
      .catch(() => {
        if (!cancelled && locationSuggestionRequestRef.current === requestId) {
          setLocationSuggestions([]);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [editingText, textDraft]);

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

  async function runClosingTaskAction(
    action: () => Promise<void>,
    message: string,
    nextClosingAction: "done" | "dismiss",
  ) {
    if (busy) return;
    setBusy(true);
    setClosingAction(nextClosingAction);
    try {
      await action();
      toast.success(message);
      await onChanged();
      onClose();
    } catch (e) {
      toast.error((e as Error).message);
      setBusy(false);
      setClosingAction(null);
    }
  }

  const markDone = () =>
    runClosingTaskAction(() => api.closeTask(current.id), "Marked done", "done");

  const dismissTask = () =>
    runClosingTaskAction(
      () => api.markNotTask(current.id),
      "Marked not a task",
      "dismiss",
    );

  async function reopenCurrentTask() {
    if (busy) return;
    const taskId = current.id;
    setBusy(true);
    const toastId = toast.loading("Re-opening task…", { duration: Infinity });

    const clearPoll = () => {
      activeReopenPoll.current = null;
      toast.dismiss(toastId);
    };

    const refreshAfterReopen = async () => {
      clearPoll();
      try {
        const saved = await api.getTask(taskId);
        syncTaskState(saved);
        setEditingText(null);
        setActivePicker(null);
        toast.success("Task re-opened");
        await onChanged();
      } catch (e) {
        toast.error((e as Error).message);
      } finally {
        setBusy(false);
      }
    };

    try {
      const { raw_input_id } = await api.reopenTask(taskId);
      const handle = pollTaskCreation(raw_input_id, {
        onSuccess: () => {
          void refreshAfterReopen();
        },
        onFailure: (message) => {
          clearPoll();
          toast.error(message);
          setBusy(false);
        },
        onTimeout: () => {
          clearPoll();
          toast.error("Task is taking longer than expected");
          setBusy(false);
        },
      });
      activeReopenPoll.current = handle;
    } catch (e) {
      clearPoll();
      toast.error((e as Error).message);
      setBusy(false);
    }
  }

  const openTextEditor = (field: TextField) => {
    setActivePicker(null);
    setEditingText(field);
    const value = current[field];
    setTextDraft(value == null ? "" : String(value));
  };

  const closeTextEditor = () => {
    locationSuggestionRequestRef.current += 1;
    setEditingText(null);
    setTextDraft("");
    setLocationSuggestions([]);
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
          kotxTask={kotxTask}
          onKotxChanged={onKotxChanged}
          onKotxActionDone={onClose}
          labels={labels}
          busy={busy}
          closingAction={closingAction}
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
          locationSuggestions={locationSuggestions}
          onSelectLocationSuggestion={setTextDraft}
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
          onMarkDone={markDone}
          onDismissTask={dismissTask}
          onReopenTask={reopenCurrentTask}
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
      className="group grid w-full min-w-0 grid-cols-[1.25rem_minmax(0,1fr)_1.25rem] items-center rounded-lg px-2 py-1 text-center text-2xl font-semibold leading-tight transition-colors hover:bg-accent/60 disabled:pointer-events-none disabled:opacity-50"
    >
      <span aria-hidden="true" />
      <span className="min-w-0 break-words">{task.title}</span>
      <Pencil className="h-4 w-4 justify-self-end text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
  );
}

function TaskSummary({
  task,
  kotxTask,
  onKotxChanged,
  onKotxActionDone,
  labels,
  busy,
  closingAction,
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
  locationSuggestions,
  onSelectLocationSuggestion,
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
  onMarkDone,
  onDismissTask,
  onReopenTask,
  onReschedule,
  onCreateGithubIssue,
}: {
  task: Task;
  kotxTask: KotxTask | null;
  onKotxChanged?: () => Promise<void> | void;
  onKotxActionDone: () => void;
  labels: Label[];
  busy: boolean;
  closingAction: "done" | "dismiss" | null;
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
  locationSuggestions: string[];
  onSelectLocationSuggestion: (value: string) => void;
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
  onMarkDone: () => void;
  onDismissTask: () => void;
  onReopenTask: () => void;
  onReschedule: () => void;
  onCreateGithubIssue: () => void;
}) {
  const labelMeta = labels.find((l) => l.name === task.label);
  const dueOverdue = isOverdue(task.due_date);
  const dueUrgent = isUrgent(task.due_date, task.estimation);
  const dueClass = dueOverdue
    ? TASK_SUMMARY_OVERDUE_BADGE_CLASS
    : dueUrgent
      ? TASK_SUMMARY_URGENT_BADGE_CLASS
      : TASK_SUMMARY_OPEN_BADGE_CLASS;

  return (
    <div className="min-h-0 flex-1 overflow-auto pr-1 pt-2">
      <div className="space-y-5">
        <div className="flex flex-wrap items-center justify-center gap-2 text-xs text-muted-foreground">
          {task.status === "open" ? (
            <TaskSummaryIconButton
              label="Mark done"
              disabled={busy}
              onClick={onMarkDone}
              className="text-muted-foreground hover:text-primary"
            >
              {closingAction === "done" ? (
                <CircleCheckBig className="h-5 w-5 text-primary" />
              ) : (
                <Circle className="h-5 w-5" />
              )}
            </TaskSummaryIconButton>
          ) : (
            <TaskSummaryIconButton
              label="Re-open task"
              disabled={busy}
              onClick={onReopenTask}
              className="text-muted-foreground hover:text-primary"
            >
              <RotateCcw className="h-5 w-5" />
            </TaskSummaryIconButton>
          )}

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
              className={cn(
                TASK_SUMMARY_BADGE_BUTTON_CLASS,
                task.label
                  ? labelChipClass(labelMeta?.color)
                  : TASK_SUMMARY_MUTED_BADGE_CLASS,
              )}
              title={labelMeta?.description ?? task.label ?? "Set label"}
            >
              <span className={TASK_SUMMARY_BADGE_CONTENT_CLASS}>
                {task.label ?? "No label"}
              </span>
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
              title={task.due_date ? `Due ${fmtDue(task.due_date)}` : "Set due date"}
              className={cn(
                TASK_SUMMARY_BADGE_BUTTON_CLASS,
                task.due_date ? dueClass : TASK_SUMMARY_MUTED_BADGE_CLASS,
              )}
            >
              <span className={TASK_SUMMARY_BADGE_CONTENT_CLASS}>
                <AlarmClock className="h-3 w-3" />
                {task.due_date ? fmtDue(task.due_date) : "No due date"}
              </span>
            </button>
          </PickerAnchor>

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
              className={cn(
                TASK_SUMMARY_BADGE_BUTTON_CLASS,
                TASK_SUMMARY_MUTED_BADGE_CLASS,
              )}
            >
              <span className={TASK_SUMMARY_BADGE_CONTENT_CLASS}>
                <Timer className="h-3 w-3" />
                {task.estimation != null ? `${task.estimation} min` : "No estimate"}
              </span>
            </button>
          </PickerAnchor>

          <button
            type="button"
            onClick={onReschedule}
            disabled={busy}
            title={
              task.scheduled_date
                ? `Reschedule task scheduled ${fmtDue(task.scheduled_date)}`
                : "Reschedule task"
            }
            aria-label={
              task.scheduled_date
                ? `Reschedule task scheduled ${fmtDue(task.scheduled_date)}`
                : "Reschedule task"
            }
            className={cn(
              TASK_SUMMARY_BADGE_BUTTON_CLASS,
              task.scheduled_date
                ? TASK_SUMMARY_SCHEDULED_BADGE_CLASS
                : TASK_SUMMARY_MUTED_BADGE_CLASS,
            )}
          >
            <span className={TASK_SUMMARY_BADGE_CONTENT_CLASS}>
              <CalendarClock className="h-3 w-3" />
              {task.scheduled_date ? fmtDue(task.scheduled_date) : "Reschedule"}
              <RefreshCw className="h-3 w-3 opacity-70" />
            </span>
          </button>

          {kotxTask && (
            <RunStatusBadge
              task={kotxTask}
              className="h-8 shrink-0 justify-center rounded-full border-transparent px-3 py-0 text-xs font-medium leading-none"
            />
          )}

          {task.status === "open" && !kotxTask && (
            <TaskSummaryIconButton
              label="Mark not a task"
              disabled={busy}
              onClick={onDismissTask}
              className="text-muted-foreground hover:text-destructive"
            >
              <Trash2
                className={cn(
                  "h-4 w-4",
                  closingAction === "dismiss" && "text-destructive",
                )}
              />
            </TaskSummaryIconButton>
          )}
        </div>

        {kotxTask && (
          <KotxRunSection
            task={kotxTask}
            onChanged={onKotxChanged ?? (() => {})}
            onActionDone={onKotxActionDone}
          />
        )}

        {/* kotx tasks carry no location/link/description — the run section
            above holds that context, so the fields stay hidden entirely. */}
        {task.kotx_task_id == null && (
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
              suggestions={locationSuggestions}
              onSelectSuggestion={onSelectLocationSuggestion}
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
          </div>
        )}

        <LinkedInputsSection inputs={task.raw_inputs ?? []} />
      </div>
    </div>
  );
}

function TaskSummaryIconButton({
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

function EditableTextBlock({
  field,
  icon,
  value,
  fallback,
  editing,
  draft,
  busy,
  multiline,
  suggestions = [],
  onEdit,
  onChange,
  onSelectSuggestion,
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
  suggestions?: string[];
  onEdit: () => void;
  onChange: (value: string) => void;
  onSelectSuggestion?: (value: string) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  if (editing) {
    return (
      <div className="rounded-lg bg-accent/40 p-2">
        {field === "location" ? (
          <LocationTextEditor
            label={TEXT_LABEL[field]}
            value={draft}
            busy={busy}
            placeholder={fallback}
            suggestions={suggestions}
            onChange={onChange}
            onSelectSuggestion={onSelectSuggestion ?? onChange}
            onCancel={onCancel}
            onSave={onSave}
          />
        ) : (
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
        )}
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
      <Pencil className="h-3.5 w-3.5 shrink-0 self-center text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
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
        <Pencil className="h-3.5 w-3.5 shrink-0 self-center text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
      </button>
      <div className="flex flex-col items-start gap-0.5">
        {task.link && (
          <a
            href={task.link}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-3 rounded-lg p-2 text-sm font-medium text-primary transition-colors hover:bg-accent/60"
          >
            <ExternalLink className="h-4 w-4 shrink-0" />
            Open provided link
          </a>
        )}
        {canCreateGithubIssue(task) && (
          <button
            type="button"
            onClick={onCreateGithubIssue}
            disabled={busy}
            className="flex items-center gap-3 rounded-lg p-2 text-sm font-medium text-primary transition-colors hover:bg-accent/60 disabled:pointer-events-none disabled:opacity-50"
          >
            <Github className="h-4 w-4 shrink-0" />
            Create GitHub issue
          </button>
        )}
      </div>
    </div>
  );
}

function LocationTextEditor({
  label,
  value,
  busy,
  placeholder,
  suggestions,
  onChange,
  onSelectSuggestion,
  onCancel,
  onSave,
}: {
  label: string;
  value: string;
  busy: boolean;
  placeholder?: string;
  suggestions: string[];
  onChange: (value: string) => void;
  onSelectSuggestion: (value: string) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  return (
    <div className="space-y-2">
      <label className="space-y-1.5">
        <span className="text-xs font-medium uppercase text-muted-foreground">
          {label}
        </span>
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={busy}
          placeholder={placeholder}
          autoFocus
        />
      </label>
      {suggestions.length > 0 && (
        <div className="rounded-md border bg-background p-1 shadow-sm">
          {suggestions.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => onSelectSuggestion(suggestion)}
              disabled={busy}
              className="flex w-full min-w-0 items-center gap-2 rounded px-2 py-1.5 text-left text-sm transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-50"
            >
              <MapPin className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 truncate">{suggestion}</span>
            </button>
          ))}
        </div>
      )}
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
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/25 p-4 sm:absolute sm:inset-auto sm:left-0 sm:top-full sm:z-20 sm:mt-2 sm:block sm:bg-transparent sm:p-0"
      onClick={onClose}
    >
      <div
        className="flex max-h-[calc(100dvh-2rem)] w-full max-w-[22rem] flex-col overflow-hidden rounded-lg border bg-card p-3 text-card-foreground shadow-lg sm:w-[min(calc(100vw-4rem),22rem)]"
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
        <div className="min-h-0 space-y-3 overflow-y-auto">
          {children}
          {footer}
        </div>
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
                  <InputStatusBadge input={input} />
                  <span className="truncate font-medium">{senderName(input)}</span>
                  <MetaDot />
                  <span className="font-medium">{fmtWhen(input.received_at)}</span>
                </div>
              </div>
              {input.source_url && (
                <OpenLink href={input.source_url}>Open source</OpenLink>
              )}
            </div>
            {hasInputDetails(input) && (
              <div className="mt-3 space-y-3 border-t pt-3 text-sm">
                <InputBody data={input} />
              </div>
            )}
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
  return (task.label === "CSEE" || task.label === "SocialAI") && !hasGithubUrl(task.link);
}

function hasGithubUrl(value: string | null) {
  if (!value) return false;
  return /^(https?:\/\/)?(www\.)?github\.com(\/|$)/i.test(value.trim());
}
