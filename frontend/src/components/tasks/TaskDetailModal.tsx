import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  AlarmClock,
  ArrowLeft,
  CalendarClock,
  CalendarDays,
  ChevronDown,
  ChevronRight,
  CircleCheckBig,
  ExternalLink,
  GitFork,
  Github,
  Link2,
  MapPin,
  Pencil,
  RefreshCw,
  RotateCcw,
  Timer,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { TaskCard } from "@/components/tasks/TaskCard";
import { DatePicker } from "@/components/ui/date-picker";
import { EstimationPicker } from "@/components/ui/estimation-picker";
import { Input } from "@/components/ui/input";
import { LabelPicker } from "@/components/ui/label-picker";
import { Markdown } from "@/components/ui/markdown";
import { Modal } from "@/components/ui/modal";
import { Textarea } from "@/components/ui/textarea";
import {
  hasInputDetails,
  InputBody,
  MetaDot,
} from "@/components/inbox/InboxCard";
import { KotxRunSection } from "@/components/tasks/KotxRunSection";
import {
  InputStatusBadge,
  RunStatusBadge,
} from "@/components/runs/RunStatusBadge";
import { useLabels } from "@/hooks/useLabels";
import { api } from "@/lib/api";
import { fmtDue, fmtWhen, isOverdue, isUrgent } from "@/lib/dates";
import { inputTitle, senderName } from "@/lib/inbox";
import { kotx, type KotxTask } from "@/lib/kotx";
import { labelChipStyle } from "@/lib/labels";
import { pollTaskCreation, type PollHandle } from "@/lib/pollTask";
import { cn } from "@/lib/utils";
import type { Label, Task, TaskRawInput } from "@/lib/types";

interface Props {
  task: Task;
  knownTasks: Task[];
  kotxTask?: KotxTask | null;
  onClose: () => void;
  onChanged: () => Promise<void> | void;
  onOpenTask: (id: string) => void;
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
const TASK_SUMMARY_UNSCHEDULED_BADGE_CLASS =
  "bg-red-500 text-white dark:bg-red-500/25 dark:text-red-100";

export function TaskDetailModal({
  task,
  knownTasks,
  kotxTask = null,
  onClose,
  onChanged,
  onOpenTask,
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
  const [subtaskDetails, setSubtaskDetails] = useState<Record<string, Task>>({});
  const [busy, setBusy] = useState(false);
  const [kotxActionPending, setKotxActionPending] = useState(false);
  const locationSuggestionRequestRef = useRef(0);
  const activeReopenPoll = useRef<PollHandle | null>(null);
  const subtaskIds = current.subtasks.map((child) => child.id).join("|");

  useEffect(() => {
    if (!subtaskIds) return;
    let cancelled = false;
    void Promise.allSettled(subtaskIds.split("|").map((id) => api.getTask(id))).then((results) => {
      if (cancelled) return;
      setSubtaskDetails((previous) => {
        const next = { ...previous };
        for (const result of results) {
          if (result.status === "fulfilled") next[result.value.id] = result.value;
        }
        return next;
      });
    });
    return () => { cancelled = true; };
  }, [subtaskIds]);

  useEffect(() => {
    setCurrent(task);
    setEditingText(null);
    setActivePicker(null);
    setDateStep("date");
    setPickerDue(task.due_date);
    setPickerEstimation(task.estimation);
    setPickerLabel(task.label ?? "");
    setKotxActionPending(false);
  }, [task]);

  useEffect(
    () => () => {
      activeReopenPoll.current?.cancel();
      activeReopenPoll.current = null;
    },
    [],
  );

  useEffect(() => {
    // Only fetch suggestions once at least one character is typed.
    if (editingText !== "location" || textDraft.trim().length < 1) {
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

  async function splitCurrentTask() {
    setBusy(true);
    try {
      const saved = await api.splitTask(current.id);
      syncTaskState(saved);
      toast.success(`Created ${saved.subtasks.length} subtasks`);
      await onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const refreshAfterSubtaskChange = async () => {
    const saved = await api.getTask(current.id);
    syncTaskState(saved);
    await onChanged();
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
    runTaskAction(
      () => api.createGithubIssue(current.id),
      "GitHub issue created",
    );

  async function runClosingTaskAction(
    action: () => Promise<void>,
    message: string,
  ) {
    if (busy) return;
    setBusy(true);
    try {
      await action();
      toast.success(message);
      onClose();
      void Promise.resolve(onChanged()).catch(() => {});
    } catch (e) {
      toast.error((e as Error).message);
      setBusy(false);
    }
  }

  const markDone = () =>
    runClosingTaskAction(() => api.closeTask(current.id), "Marked done");

  const dismissTask = () =>
    runClosingTaskAction(
      () => api.markNotTask(current.id),
      "Marked not a task",
    );

  // kotx tasks: done is handled through kotx, so the check-off dismisses —
  // discard the run upstream, or drop the 007 task when the run is already
  // terminal.
  const dismissRun = () => {
    if (!kotxTask) return;
    runClosingTaskAction(
      async () => {
        if (kotxTask.canDiscard) {
          await kotx.discard(kotxTask.id);
          await onKotxChanged?.();
        } else {
          await api.markNotTask(current.id);
        }
      },
      kotxTask.canDiscard ? "Run discarded" : "Marked not a task",
    );
  };

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
    else if (field === "description")
      patch = { description: normalizeOptional(textDraft) };
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

  const saveActiveEdit = async () => {
    if (busy) return;
    if (editingText) {
      await saveTextEditor(editingText);
      return;
    }
    if (!activePicker) return;
    const patch: Partial<Task> = activePicker === "due_date"
      ? { due_date: pickerDue }
      : activePicker === "estimation"
        ? { estimation: pickerEstimation }
        : { label: pickerLabel || null };
    const saved = await savePatch(patch);
    if (saved) setActivePicker(null);
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={current.title}
      titleLabel={current.title}
      backdropClassName="max-sm:p-0"
      className="h-[760px] max-h-[calc(100dvh-2rem)] max-w-3xl max-sm:h-dvh max-sm:max-h-dvh max-sm:max-w-none max-sm:rounded-none max-sm:border-0 max-sm:p-0"
      header={
        <div className="relative z-30 flex h-[72px] shrink-0 items-center justify-between border-b bg-card px-4 sm:mb-3 sm:-mx-4 sm:-mt-4 sm:rounded-t-xl">
          <div className="flex items-center gap-1">
            <Button type="button" size="icon" variant="ghost" onClick={onClose} aria-label={editingText || activePicker ? "Cancel and close" : "Back"} className="h-12 w-12 shrink-0">
              <ArrowLeft className="h-5 w-5" />
            </Button>
            {(editingText || activePicker) && (
              <Button type="button" size="sm" onClick={() => { void saveActiveEdit(); }} disabled={busy}>
                Save
              </Button>
            )}
          </div>
          <div className="flex items-center gap-1">
            {current.status === "open" && !current.is_container && (
              <Button
                type="button"
                size="icon"
                aria-label={
                  kotxTask
                    ? kotxTask.canDiscard
                      ? "Dismiss run"
                      : "Mark not a task"
                    : "Mark done"
                }
                disabled={busy}
                onClick={kotxTask ? dismissRun : markDone}
                className="h-12 w-12 shrink-0 rounded-full"
              >
                <CircleCheckBig className="h-5 w-5" />
              </Button>
            )}
            {current.status === "open" &&
              !current.is_container &&
              !current.parent_task_id &&
              !kotxTask && (
                <TaskSummaryIconButton
                  label="Split into subtasks"
                  disabled={busy}
                  onClick={splitCurrentTask}
                >
                  <GitFork className="h-5 w-5" />
                </TaskSummaryIconButton>
              )}
            {current.status === "open" &&
              !current.is_container &&
              !kotxTask && (
                <TaskSummaryIconButton
                  label="Mark not a task"
                  disabled={busy}
                  onClick={dismissTask}
                  className="hover:text-destructive"
                >
                  <Trash2 className="h-5 w-5" />
                </TaskSummaryIconButton>
              )}
          </div>
        </div>
      }
    >
      <TaskSummary
          title={
            <TaskTitleHeader
              task={current}
              editing={editingText === "title"}
              draft={textDraft}
              busy={busy}
              onEdit={() => openTextEditor("title")}
              onChange={setTextDraft}
            />
          }
          task={current}
          knownTasks={knownTasks}
          subtaskDetails={subtaskDetails}
          onSubtaskChanged={refreshAfterSubtaskChange}
          kotxTask={kotxTask}
          onKotxChanged={onKotxChanged}
          onKotxActionDone={onClose}
          onOpenTask={onOpenTask}
          labels={labels}
          busy={busy}
          kotxActionPending={kotxActionPending}
          editingText={editingText}
          textDraft={textDraft}
          activePicker={activePicker}
          dateStep={dateStep}
          pickerDue={pickerDue}
          pickerEstimation={pickerEstimation}
          pickerLabel={pickerLabel}
          onEditText={openTextEditor}
          onChangeText={setTextDraft}
          locationSuggestions={locationSuggestions}
          onSelectLocationSuggestion={setTextDraft}
          onEditPicker={openPicker}
          onDateStepChange={setDateStep}
          onPickerDueChange={setPickerDue}
          onPickerEstimationChange={setPickerEstimation}
          onPickerLabelChange={setPickerLabel}
          onReopenTask={reopenCurrentTask}
          onReschedule={rescheduleCurrent}
          onCreateGithubIssue={createGithubIssue}
          onKotxActionPendingChange={setKotxActionPending}
      />
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
}: {
  task: Task;
  editing: boolean;
  draft: string;
  busy: boolean;
  onEdit: () => void;
  onChange: (value: string) => void;
}) {
  if (editing) {
    return (
      <div className="text-left text-sm font-normal leading-normal">
        <InlineTextEditor
          label={TEXT_LABEL.title}
          value={draft}
          busy={busy}
          onChange={onChange}
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
      className="group flex w-full min-w-0 items-start gap-2 rounded-lg px-2 py-1 text-left text-2xl font-semibold leading-tight transition-colors hover:bg-accent/60 disabled:pointer-events-none disabled:opacity-50"
    >
      <span className="min-w-0 flex-1 break-words">{task.title}</span>
      <Pencil className="mt-1 h-4 w-4 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
  );
}

function TaskSummary({
  title,
  task,
  knownTasks,
  subtaskDetails,
  onSubtaskChanged,
  kotxTask,
  onKotxChanged,
  onKotxActionDone,
  onOpenTask,
  labels,
  busy,
  kotxActionPending,
  editingText,
  textDraft,
  activePicker,
  dateStep,
  pickerDue,
  pickerEstimation,
  pickerLabel,
  onEditText,
  onChangeText,
  locationSuggestions,
  onSelectLocationSuggestion,
  onEditPicker,
  onDateStepChange,
  onPickerDueChange,
  onPickerEstimationChange,
  onPickerLabelChange,
  onReopenTask,
  onReschedule,
  onCreateGithubIssue,
  onKotxActionPendingChange,
}: {
  title: ReactNode;
  task: Task;
  knownTasks: Task[];
  subtaskDetails: Record<string, Task>;
  onSubtaskChanged: () => Promise<void>;
  kotxTask: KotxTask | null;
  onKotxChanged?: () => Promise<void> | void;
  onKotxActionDone: () => void;
  onOpenTask: (id: string) => void;
  labels: Label[];
  busy: boolean;
  kotxActionPending: boolean;
  editingText: TextField | null;
  textDraft: string;
  activePicker: PickerField | null;
  dateStep: "date" | "time";
  pickerDue: string | null;
  pickerEstimation: number | null;
  pickerLabel: string;
  onEditText: (field: TextField) => void;
  onChangeText: (value: string) => void;
  locationSuggestions: string[];
  onSelectLocationSuggestion: (value: string) => void;
  onEditPicker: (field: PickerField) => void;
  onDateStepChange: (step: "date" | "time") => void;
  onPickerDueChange: (value: string | null) => void;
  onPickerEstimationChange: (value: number | null) => void;
  onPickerLabelChange: (value: string) => void;
  onReopenTask: () => void;
  onReschedule: () => void;
  onCreateGithubIssue: () => void;
  onKotxActionPendingChange: (pending: boolean) => void;
}) {
  const labelMeta = labels.find((l) => l.name === task.label);
  const dueOverdue = isOverdue(task.due_date);
  const dueUrgent = isUrgent(task.due_date, task.estimation);
  const dueClass = dueOverdue
    ? TASK_SUMMARY_OVERDUE_BADGE_CLASS
    : dueUrgent
      ? TASK_SUMMARY_URGENT_BADGE_CLASS
      : TASK_SUMMARY_OPEN_BADGE_CLASS;
  const scheduledBadgeText =
    task.schedule_status === "unscheduled"
      ? "Not scheduled"
      : task.scheduled_date
        ? fmtDue(task.scheduled_date)
        : "Reschedule";
  const scheduledBadgeLabel =
    task.schedule_status === "unscheduled"
      ? "Not scheduled"
      : task.scheduled_date
        ? `Reschedule task scheduled ${fmtDue(task.scheduled_date)}`
        : "Reschedule task";

  return (
    <div className="min-h-0 flex-1 overflow-auto px-3 pb-[max(1rem,env(safe-area-inset-bottom))] pt-3 sm:-mx-4 sm:px-4">
      <div className="space-y-5">
        {title}
        <div className="flex flex-wrap items-center justify-center gap-2 text-xs text-muted-foreground">
          {task.status !== "open" && !kotxTask ? (
            <TaskSummaryIconButton
              label="Re-open task"
              disabled={busy}
              onClick={onReopenTask}
              className="text-muted-foreground hover:text-primary"
            >
              <RotateCcw className="h-5 w-5" />
            </TaskSummaryIconButton>
          ) : null}

          <PickerAnchor
            open={activePicker === "label"}
            panel={
              <InlinePickerPanel title="Label">
                <LabelPicker
                  value={pickerLabel}
                  onChange={onPickerLabelChange}
                  labels={labels}
                  defaultOpen
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
                TASK_SUMMARY_MUTED_BADGE_CLASS,
              )}
              style={task.label ? labelChipStyle(labelMeta?.color) : undefined}
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
                onEditDate={
                  dateStep === "time"
                    ? () => onDateStepChange("date")
                    : undefined
                }
                footer={
                  pickerDue && !task.is_container ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="w-full"
                      onClick={() => onPickerDueChange(null)}
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
              title={
                task.due_date ? `Due ${fmtDue(task.due_date)}` : "Set due date"
              }
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
            open={!task.is_container && activePicker === "estimation"}
            panel={
              <InlinePickerPanel title="Estimate">
                <EstimationPicker
                  value={pickerEstimation}
                  onChange={onPickerEstimationChange}
                />
              </InlinePickerPanel>
            }
          >
            <button
              type="button"
              onClick={() => onEditPicker("estimation")}
              disabled={busy || task.is_container}
              title={
                task.is_container
                  ? "Duration is set by subtasks"
                  : "Set duration"
              }
              className={cn(
                TASK_SUMMARY_BADGE_BUTTON_CLASS,
                TASK_SUMMARY_MUTED_BADGE_CLASS,
              )}
            >
              <span className={TASK_SUMMARY_BADGE_CONTENT_CLASS}>
                <Timer className="h-3 w-3" />
                {task.estimation != null
                  ? `${task.estimation} min`
                  : "No estimate"}
              </span>
            </button>
          </PickerAnchor>

          {!task.is_container && (
            <button
              type="button"
              onClick={onReschedule}
              disabled={busy}
              title={scheduledBadgeLabel}
              aria-label={scheduledBadgeLabel}
              className={cn(
                TASK_SUMMARY_BADGE_BUTTON_CLASS,
                task.schedule_status === "unscheduled"
                  ? TASK_SUMMARY_UNSCHEDULED_BADGE_CLASS
                  : task.scheduled_date
                    ? TASK_SUMMARY_SCHEDULED_BADGE_CLASS
                    : TASK_SUMMARY_MUTED_BADGE_CLASS,
              )}
            >
              <span className={TASK_SUMMARY_BADGE_CONTENT_CLASS}>
                <CalendarClock className="h-3 w-3" />
                {scheduledBadgeText}
                <RefreshCw className="h-3 w-3 opacity-70" />
              </span>
            </button>
          )}

          {kotxTask && (
            <RunStatusBadge
              task={kotxTask}
              className="h-8 shrink-0 justify-center rounded-full border-transparent px-3 py-0 text-xs font-medium leading-none"
            />
          )}
        </div>

        {task.subtasks.length > 0 && (
          <section className="space-y-2">
            <h3 className="text-sm font-semibold">Subtasks</h3>
            {task.subtasks.map((child) => {
              const fullTask = knownTasks.find((candidate) => candidate.id === child.id) ?? subtaskDetails[child.id];
              return fullTask ? (
                <TaskCard
                  key={child.id}
                  task={{ ...fullTask, title: child.title, status: child.status, due_date: child.due_date, estimation: child.estimation }}
                  onChanged={onSubtaskChanged}
                  onKotxChanged={onSubtaskChanged}
                  onOpen={onOpenTask}
                />
              ) : (
                <div key={child.id} className="rounded-xl border bg-card p-3 text-sm text-muted-foreground" role="status">
                  Loading {child.title}…
                </div>
              );
            })}
          </section>
        )}

        {kotxTask && (
          <KotxRunSection
            task={kotxTask}
            onChanged={onKotxChanged ?? (() => {})}
            onActionDone={onKotxActionDone}
            onActionPendingChange={onKotxActionPendingChange}
          />
        )}

        {/* kotx tasks carry no location/link/description — the run section
            above holds that context, so the fields stay hidden entirely. */}
        {task.kotx_task_id == null && (
          <div className="space-y-1.5">
            <div
              className={cn(
                "grid gap-1.5",
                !task.is_container && "grid-cols-2",
              )}
            >
              {!task.is_container && (
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
                />
              )}
              <LinksSection
                task={task}
                editing={editingText === "link"}
                draft={textDraft}
                busy={busy}
                onEdit={() => onEditText("link")}
                onChange={onChangeText}
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
              markdown
              onEdit={() => onEditText("description")}
              onChange={onChangeText}
            />
          </div>
        )}

        <LinkedInputsSection
          inputs={task.raw_inputs ?? []}
          currentTaskId={task.id}
          parentTaskId={task.parent_task_id}
          onOpenTask={onOpenTask}
          muted={kotxActionPending}
        />
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
  markdown,
  suggestions = [],
  onEdit,
  onChange,
  onSelectSuggestion,
}: {
  field: TextField;
  icon?: ReactNode;
  value: string | null;
  fallback: string;
  editing: boolean;
  draft: string;
  busy: boolean;
  multiline?: boolean;
  markdown?: boolean;
  suggestions?: string[];
  onEdit: () => void;
  onChange: (value: string) => void;
  onSelectSuggestion?: (value: string) => void;
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
          />
        ) : (
          <InlineTextEditor
            label={TEXT_LABEL[field]}
            value={draft}
            busy={busy}
            multiline={multiline}
            placeholder={fallback}
            onChange={onChange}
          />
        )}
      </div>
    );
  }

  // Rendered markdown can contain links, so it can't sit inside the click-to-
  // edit <button> (nested <a>, and clicks would trigger an edit). Lay it out as
  // a plain block with a dedicated edit pencil instead. Empty descriptions fall
  // through to the button below so "Add description" stays one tap.
  if (markdown && value) {
    return (
      <div className="group relative flex w-full min-w-0 items-start gap-3 rounded-lg p-2 transition-colors hover:bg-accent/60">
        {icon && (
          <span className="mt-0.5 shrink-0 text-muted-foreground">{icon}</span>
        )}
        <span className="min-w-0 flex-1">
          <span className="block text-xs font-medium uppercase text-muted-foreground">
            {TEXT_LABEL[field]}
          </span>
          <Markdown content={value} className="mt-1" />
        </span>
        <button
          type="button"
          onClick={onEdit}
          disabled={busy}
          aria-label={`Edit ${TEXT_LABEL[field]}`}
          className="shrink-0 self-center rounded-md p-1 text-muted-foreground transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-50"
        >
          <Pencil className="h-3.5 w-3.5 opacity-0 transition-opacity group-hover:opacity-100" />
        </button>
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
      {icon && (
        <span className="mt-0.5 shrink-0 text-muted-foreground">{icon}</span>
      )}
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
          <span className="block text-sm text-muted-foreground">
            {fallback}
          </span>
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
  onCreateGithubIssue,
}: {
  task: Task;
  editing: boolean;
  draft: string;
  busy: boolean;
  onEdit: () => void;
  onChange: (value: string) => void;
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
            {task.link || "Add link"}
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
}: {
  label: string;
  value: string;
  busy: boolean;
  placeholder?: string;
  suggestions: string[];
  onChange: (value: string) => void;
  onSelectSuggestion: (value: string) => void;
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
}: {
  label: string;
  value: string;
  busy: boolean;
  multiline?: boolean;
  placeholder?: string;
  inputClassName?: string;
  onChange: (value: string) => void;
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
  onEditDate,
}: {
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  onEditDate?: () => void;
}) {
  return (
    <div
      className="fixed inset-x-0 bottom-0 top-[72px] z-20 flex items-start justify-center overflow-y-auto bg-card p-4 sm:absolute sm:inset-auto sm:left-0 sm:top-full sm:mt-2 sm:block sm:overflow-visible sm:bg-transparent sm:p-0"
    >
      <div
        className="flex max-h-[calc(100dvh-2rem)] w-full max-w-[22rem] flex-col overflow-hidden rounded-lg border bg-card p-3 text-card-foreground shadow-lg sm:w-[min(calc(100vw-4rem),22rem)]"
      >
        <div className="mb-2 grid grid-cols-[1.75rem_1fr_1.75rem] items-center">
          <div>
            {onEditDate && (
              <button
                type="button"
                aria-label="Edit date"
                title="Edit date"
                onClick={onEditDate}
                className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              >
                <CalendarDays className="h-4 w-4" />
              </button>
            )}
          </div>
          <div className="truncate text-center text-sm font-semibold">
            {title}
          </div>
          <span aria-hidden="true" />
        </div>
        <div className="min-h-0 space-y-3 overflow-y-auto">
          {children}
          {footer}
        </div>
      </div>
    </div>
  );
}

function LinkedInputsSection({
  inputs,
  currentTaskId,
  parentTaskId,
  onOpenTask,
  muted = false,
}: {
  inputs: TaskRawInput[];
  currentTaskId: string;
  parentTaskId: string | null;
  onOpenTask: (id: string) => void;
  muted?: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (inputs.length === 0) return null;

  return (
    <section
      className={cn(
        "space-y-2 border-t pt-3 transition-opacity",
        muted && "pointer-events-none opacity-50",
      )}
      aria-disabled={muted}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between text-left text-xs font-medium uppercase text-muted-foreground"
      >
        <span>Linked inputs ({inputs.length})</span>
        {open ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
      </button>
      {open && (
        <div className="space-y-2">
          {inputs.map((input) => {
            const targetTaskId =
              input.source === "subtask" && parentTaskId
                ? parentTaskId
                : input.task_id && input.task_id !== currentTaskId
                  ? input.task_id
                  : null;
            return (
              <div
                key={input.id}
                role={targetTaskId ? "button" : undefined}
                tabIndex={targetTaskId ? 0 : undefined}
                aria-label={
                  targetTaskId
                    ? `Open ${targetTaskId === parentTaskId ? "parent task" : "linked task"}: ${targetTaskId === parentTaskId && typeof input.source_metadata.parent_title === "string" ? input.source_metadata.parent_title : (input.task_title ?? inputTitle(input))}`
                    : undefined
                }
                onClick={(event) => {
                  if (!targetTaskId) return;
                  const interactive = (event.target as HTMLElement).closest(
                    "a,button,[role='button']",
                  );
                  if (interactive && interactive !== event.currentTarget)
                    return;
                  onOpenTask(targetTaskId);
                }}
                onKeyDown={(event) => {
                  if (!targetTaskId || event.target !== event.currentTarget)
                    return;
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onOpenTask(targetTaskId);
                  }
                }}
                className={cn(
                  "rounded-lg border p-3",
                  targetTaskId &&
                    "cursor-pointer transition-colors hover:bg-accent/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary",
                )}
              >
                <div className="flex min-w-0 items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">
                      {inputTitle(input)}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                      <InputStatusBadge input={input} />
                      <span className="truncate font-medium">
                        {senderName(input)}
                      </span>
                      <MetaDot />
                      <span className="font-medium">
                        {fmtWhen(input.received_at)}
                      </span>
                    </div>
                  </div>
                  {input.source_url && (
                    <OpenLink href={input.source_url} disabled={muted}>
                      Open source
                    </OpenLink>
                  )}
                </div>
                {hasInputDetails(input) && (
                  <div className="mt-3 space-y-3 border-t pt-3 text-sm">
                    <InputBody data={input} onOpenTask={onOpenTask} />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function OpenLink({
  href,
  disabled = false,
  children,
}: {
  href: string;
  disabled?: boolean;
  children: ReactNode;
}) {
  if (disabled) {
    return (
      <span
        aria-disabled="true"
        className="inline-flex max-w-full items-center gap-1 rounded-md px-2 py-1 text-sm font-medium text-primary"
      >
        <ExternalLink className="h-3.5 w-3.5 shrink-0" />
        <span className="min-w-0 truncate">{children}</span>
      </span>
    );
  }

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
  return (
    (task.label === "CSEE" || task.label === "SocialAI") &&
    !hasGithubUrl(task.link)
  );
}

function hasGithubUrl(value: string | null) {
  if (!value) return false;
  return /^(https?:\/\/)?(www\.)?github\.com(\/|$)/i.test(value.trim());
}
