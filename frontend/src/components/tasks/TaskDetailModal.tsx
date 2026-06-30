import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  ChevronLeft,
  ExternalLink,
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
import { fmtDue, fmtWhen } from "@/lib/dates";
import { labelChipClass } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { Task } from "@/lib/types";

interface Props {
  task: Task;
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}

type Picker = "summary" | "due_date" | "estimation" | "label";

export function TaskDetailModal({ task, onClose, onChanged }: Props) {
  const labels = useLabels();
  const [current, setCurrent] = useState(task);
  const [draft, setDraft] = useState(() => toDraft(task));
  const [picker, setPicker] = useState<Picker>("summary");
  const [dateStep, setDateStep] = useState<"date" | "time">("date");
  const [pickerDue, setPickerDue] = useState(task.due_date);
  const [pickerEstimation, setPickerEstimation] = useState(task.estimation);
  const [pickerLabel, setPickerLabel] = useState(task.label ?? "");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setCurrent(task);
    setDraft(toDraft(task));
    setPickerDue(task.due_date);
    setPickerEstimation(task.estimation);
    setPickerLabel(task.label ?? "");
  }, [task]);

  useEffect(() => {
    if (!loading) return;
    const timer = window.setTimeout(() => setLoading(false), 120);
    return () => window.clearTimeout(timer);
  }, [loading]);

  const labelMeta = labels.find((l) => l.name === current.label);
  const dirty = useMemo(
    () =>
      draft.title.trim() !== current.title ||
      normalizeOptional(draft.description) !== current.description ||
      normalizeOptional(draft.link) !== current.link ||
      normalizeOptional(draft.location) !== current.location,
    [current, draft],
  );

  async function savePatch(patch: Partial<Task>, message = "Saved") {
    setBusy(true);
    try {
      const saved = await api.updateTask(current.id, patch);
      setCurrent(saved);
      setDraft(toDraft(saved));
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

  const saveSummary = async () => {
    const title = draft.title.trim();
    if (!title) {
      toast.error("Title is required");
      return;
    }
    await savePatch({
      title,
      description: normalizeOptional(draft.description),
      link: normalizeOptional(draft.link),
      location: normalizeOptional(draft.location),
    });
  };

  const openPicker = (next: Picker) => {
    setPicker(next);
    setDateStep("date");
    setPickerDue(current.due_date);
    setPickerEstimation(current.estimation);
    setPickerLabel(current.label ?? "");
  };

  const title = loading ? "Task details" : current.title;

  return (
    <Modal
      open
      onClose={onClose}
      title={title}
      titleClassName="text-lg"
      className="h-[760px] max-h-[calc(100dvh-2rem)] max-w-3xl"
      leftAction={
        picker === "due_date" && dateStep === "time" ? (
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
      {loading ? (
        <div className="min-h-0 flex-1 overflow-hidden">
          <ModalSkeleton />
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 gap-4 overflow-hidden md:grid-cols-[1fr_19rem]">
          <div className="min-h-0 overflow-auto pr-1">
            <div className="space-y-3">
              <Field label="Title">
                <Input
                  value={draft.title}
                  onChange={(e) =>
                    setDraft((prev) => ({ ...prev, title: e.target.value }))
                  }
                  disabled={busy}
                />
              </Field>
              <Field label="Description">
                <Textarea
                  value={draft.description}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      description: e.target.value,
                    }))
                  }
                  disabled={busy}
                  className="h-28 resize-none"
                />
              </Field>
              <Field label="Source link">
                <div className="flex gap-2">
                  <Input
                    value={draft.link}
                    onChange={(e) =>
                      setDraft((prev) => ({ ...prev, link: e.target.value }))
                    }
                    disabled={busy}
                    placeholder="https://..."
                  />
                  {current.link && (
                    <Button asChild variant="outline" size="icon">
                      <a
                        href={current.link}
                        target="_blank"
                        rel="noopener noreferrer"
                        aria-label="Open source"
                        title="Open source"
                      >
                        <ExternalLink className="h-4 w-4" />
                      </a>
                    </Button>
                  )}
                </div>
              </Field>
              <Field label="Location">
                <Input
                  value={draft.location}
                  onChange={(e) =>
                    setDraft((prev) => ({ ...prev, location: e.target.value }))
                  }
                  disabled={busy}
                />
              </Field>

              <div className="flex justify-end gap-2 pt-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setDraft(toDraft(current))}
                  disabled={busy || !dirty}
                >
                  Reset
                </Button>
                <Button
                  type="button"
                  size="sm"
                  onClick={saveSummary}
                  disabled={busy || !dirty}
                >
                  Save
                </Button>
              </div>

              <div className="grid gap-2 border-t pt-3 text-sm sm:grid-cols-2">
                <EditableMeta
                  label="Due"
                  onEdit={() => openPicker("due_date")}
                  disabled={busy}
                >
                  {current.due_date ? fmtDue(current.due_date) : "None"}
                </EditableMeta>
                <EditableMeta
                  label="Estimation"
                  onEdit={() => openPicker("estimation")}
                  disabled={busy}
                >
                  {current.estimation != null ? (
                    <span className="inline-flex items-center gap-1">
                      <Timer className="h-3.5 w-3.5" />
                      {current.estimation} min
                    </span>
                  ) : (
                    "None"
                  )}
                </EditableMeta>
                <EditableMeta
                  label="Label"
                  onEdit={() => openPicker("label")}
                  disabled={busy}
                >
                  {current.label ? (
                    <span
                      className={cn(
                        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium",
                        labelChipClass(labelMeta?.color),
                      )}
                      title={labelMeta?.description ?? current.label}
                    >
                      {current.label}
                    </span>
                  ) : (
                    "None"
                  )}
                </EditableMeta>
                <Meta label="Status">
                  <Badge variant={current.status}>{current.status}</Badge>
                </Meta>
                <Meta label="Source">
                  {current.is_manual ? "Manual" : "Extracted"}
                </Meta>
                <Meta label="Created">{fmtWhen(current.created_at)}</Meta>
                <Meta label="Updated">{fmtWhen(current.updated_at)}</Meta>
              </div>
            </div>
          </div>

          <aside className="min-h-0 overflow-auto rounded-lg border bg-muted/20 p-3">
            {picker === "summary" && <SummaryPanel task={current} />}
            {picker === "due_date" && (
              <PickerPanel title="Due date" onBack={() => setPicker("summary")}>
                <DatePicker
                  value={pickerDue}
                  onChange={setPickerDue}
                  onSave={async () => {
                    const saved = await savePatch({ due_date: pickerDue });
                    if (saved) setPicker("summary");
                  }}
                  step={dateStep}
                  onStepChange={setDateStep}
                />
                {pickerDue && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="mt-2 w-full"
                    onClick={async () => {
                      const saved = await savePatch({ due_date: null });
                      if (saved) setPicker("summary");
                    }}
                    disabled={busy}
                  >
                    Clear due date
                  </Button>
                )}
              </PickerPanel>
            )}
            {picker === "estimation" && (
              <PickerPanel title="Estimation" onBack={() => setPicker("summary")}>
                <EstimationPicker
                  value={pickerEstimation}
                  onChange={setPickerEstimation}
                  onSave={async () => {
                    const saved = await savePatch({
                      estimation: pickerEstimation,
                    });
                    if (saved) setPicker("summary");
                  }}
                />
              </PickerPanel>
            )}
            {picker === "label" && (
              <PickerPanel title="Label" onBack={() => setPicker("summary")}>
                <LabelPicker
                  value={pickerLabel}
                  onChange={setPickerLabel}
                  onSave={async () => {
                    const saved = await savePatch({
                      label: pickerLabel || null,
                    });
                    if (saved) setPicker("summary");
                  }}
                  labels={labels}
                />
              </PickerPanel>
            )}
          </aside>
        </div>
      )}
    </Modal>
  );
}

function toDraft(task: Task) {
  return {
    title: task.title,
    description: task.description ?? "",
    link: task.link ?? "",
    location: task.location ?? "",
  };
}

function normalizeOptional(value: string) {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0 rounded-md border bg-background p-2">
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className="mt-1 min-w-0 break-words">{children}</div>
    </div>
  );
}

function EditableMeta({
  label,
  children,
  onEdit,
  disabled,
}: {
  label: string;
  children: ReactNode;
  onEdit: () => void;
  disabled: boolean;
}) {
  return (
    <div className="min-w-0 rounded-md border bg-background p-2">
      <div className="mb-1 flex items-center justify-between gap-2">
        <div className="text-xs font-medium text-muted-foreground">{label}</div>
        <button
          type="button"
          aria-label={`Edit ${label}`}
          title={`Edit ${label}`}
          onClick={onEdit}
          disabled={disabled}
          className="inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
        >
          <Pencil className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className="min-w-0 break-words">{children}</div>
    </div>
  );
}

function PickerPanel({
  title,
  children,
  onBack,
}: {
  title: string;
  children: ReactNode;
  onBack: () => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        <Button type="button" variant="ghost" size="sm" onClick={onBack}>
          Done
        </Button>
      </div>
      {children}
    </div>
  );
}

function SummaryPanel({ task }: { task: Task }) {
  return (
    <div className="space-y-3 text-sm">
      <h3 className="font-semibold">Task summary</h3>
      {task.description ? (
        <p className="whitespace-pre-wrap break-words text-muted-foreground">
          {task.description}
        </p>
      ) : (
        <p className="text-muted-foreground">No description.</p>
      )}
      {task.location && (
        <div className="inline-flex max-w-full items-center gap-1.5 text-muted-foreground">
          <MapPin className="h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 break-words">{task.location}</span>
        </div>
      )}
      {task.link && (
        <a
          href={task.link}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex max-w-full items-center gap-1.5 text-primary hover:underline"
        >
          <ExternalLink className="h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 truncate">Open source</span>
        </a>
      )}
    </div>
  );
}
