import { useEffect, useRef, useState, type CSSProperties, type FormEvent, type ReactNode } from "react";
import { AlarmClock, ArrowLeft, History, Hourglass, Plus, Tag, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DatePicker } from "@/components/ui/date-picker";
import { EstimationPicker } from "@/components/ui/estimation-picker";
import { Input } from "@/components/ui/input";
import { LabelPicker } from "@/components/ui/label-picker";
import { Modal } from "@/components/ui/modal";
import { LogWorkModal } from "@/components/work/LogWorkModal";
import { useLabels } from "@/hooks/useLabels";
import { api } from "@/lib/api";
import { fmtDue } from "@/lib/dates";
import { labelChipStyle } from "@/lib/labels";
import { toast } from "@/lib/toast";
import { pollTaskCreation, type PollHandle } from "@/lib/pollTask";
import type { Task } from "@/lib/types";
import { cn } from "@/lib/utils";
import { fmtMinutes } from "@/lib/work";

interface Props {
  onCreated: () => Promise<void> | void;
}

type DraftField = "due_date" | "label" | "estimation";
type Draft = Pick<Task, DraftField>;

const EMPTY_DRAFT: Draft = { due_date: null, label: null, estimation: null };
const PICKER_TITLE: Record<DraftField, string> = {
  due_date: "Due date",
  label: "Label",
  estimation: "Estimation",
};

export function Composer({ onCreated }: Props) {
  const [value, setValue] = useState("");
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [picker, setPicker] = useState<DraftField | null>(null);
  const [dateStep, setDateStep] = useState<"date" | "time">("date");
  const [loggingWork, setLoggingWork] = useState(false);
  const labels = useLabels();
  const inputRef = useRef<HTMLInputElement>(null);
  const postingText = useRef<Set<string>>(new Set());
  // Stop in-flight polls when the component unmounts.
  const activePolls = useRef<Map<PollHandle, string | number>>(new Map());
  const hasText = value.trim() !== "";

  useEffect(
    () => () => {
      activePolls.current.forEach((toastId, handle) => {
        handle.cancel();
        toast.dismiss(toastId);
      });
      activePolls.current.clear();
    },
    [],
  );

  const trackPoll = (rawInputId: string, toastId: string | number) => {
    let handle: PollHandle | null = null;
    const finish = (run: () => void) => {
      toast.dismiss(toastId);
      run();
      if (handle) activePolls.current.delete(handle);
    };
    handle = pollTaskCreation(rawInputId, {
      onSuccess: () =>
        finish(() => {
          toast.success("Task saved");
          void Promise.resolve(onCreated()).catch(() => {});
        }),
      onFailure: (message) => finish(() => {
        toast.error(message);
      }),
      onTimeout: () =>
        finish(() => toast.error("Task is taking longer than expected")),
    });
    activePolls.current.set(handle, toastId);
  };

  const clearInput = () => {
    setValue("");
    setDraft(EMPTY_DRAFT);
    if (inputRef.current) inputRef.current.value = "";
  };

  const setDraftField = (fields: Partial<Draft>) => setDraft((current) => ({ ...current, ...fields }));

  const openPicker = (field: DraftField) => {
    setDateStep("date");
    setPicker(field);
  };

  const closePicker = () => {
    setPicker(null);
    // A phone would pop the keyboard back over the pills.
    if (window.matchMedia("(pointer: fine)").matches) inputRef.current?.focus();
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const text = inputRef.current?.value.trim() ?? value.trim();
    if (!text || postingText.current.has(text)) return;
    const fields = draft;
    postingText.current.add(text);
    clearInput();
    inputRef.current?.focus();
    // Show the loading toast immediately — the POST itself takes a moment,
    // so without this the user gets no feedback until polling starts.
    const toastId = toast.loading("Saving task…", { duration: Infinity });
    try {
      const { raw_input_id } = await api.createTask(text, fields);
      trackPoll(raw_input_id, toastId);
    } catch (err) {
      if (!inputRef.current?.value.trim()) {
        setValue(text);
        setDraft(fields);
      }
      toast.dismiss(toastId);
      toast.error((err as Error).message);
    } finally {
      postingText.current.delete(text);
    }
  };

  const draftLabel = labels.find((label) => label.name === draft.label);

  return (
    <>
      {/* Same translucent bar as the Topbar, reaching into the bottom safe area. */}
      <div className="fixed inset-x-0 bottom-0 z-40 bg-background/90 pb-[env(safe-area-inset-bottom)] backdrop-blur-xl backdrop-saturate-150 supports-[backdrop-filter]:bg-background/75">
        <form onSubmit={submit} autoComplete="off">
          <div className="mx-auto max-w-2xl px-4 py-3">
            {hasText && (
              <div className="-mx-4 mb-2 flex gap-2 overflow-x-auto px-4 [scrollbar-width:none]">
                <DraftPill
                  name="Due date"
                  icon={<AlarmClock className="h-3.5 w-3.5" />}
                  text={draft.due_date ? fmtDue(draft.due_date) : null}
                  className="bg-emerald-100 text-emerald-800 dark:bg-emerald-500/20 dark:text-emerald-200"
                  onOpen={() => openPicker("due_date")}
                  onClear={() => setDraftField({ due_date: null })}
                />
                <DraftPill
                  name="Label"
                  icon={<Tag className="h-3.5 w-3.5" />}
                  text={draft.label}
                  className="bg-muted text-foreground"
                  style={labelChipStyle(draftLabel?.color)}
                  onOpen={() => openPicker("label")}
                  onClear={() => setDraftField({ label: null })}
                />
                <DraftPill
                  name="Estimation"
                  icon={<Hourglass className="h-3.5 w-3.5" />}
                  text={draft.estimation ? fmtMinutes(draft.estimation) : null}
                  className="bg-blue-100 text-blue-800 dark:bg-blue-500/20 dark:text-blue-200"
                  onOpen={() => openPicker("estimation")}
                  onClear={() => setDraftField({ estimation: null })}
                />
              </div>
            )}
            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="secondary"
                onClick={() => setLoggingWork(true)}
                disabled={!hasText}
                aria-label="Log work"
                title="Log work you already did"
                className="h-12 w-12 shrink-0 p-0 shadow-sm"
              >
                <History className="h-5 w-5" />
              </Button>
              <div className="flex h-12 min-w-0 flex-1 items-center rounded-full bg-secondary px-4 shadow-sm">
                <Input
                  ref={inputRef}
                  value={value}
                  onChange={(e) => {
                    setValue(e.target.value);
                    if (!e.target.value.trim()) setDraft(EMPTY_DRAFT);
                  }}
                  placeholder="Add a task…"
                  enterKeyHint="send"
                  autoCapitalize="sentences"
                  autoComplete="off"
                  autoCorrect="off"
                  // Suppress browser + password-manager autofill overlays on this field.
                  name="task-title"
                  data-1p-ignore
                  data-lpignore="true"
                  data-form-type="other"
                  className="h-10 min-w-0 flex-1 border-0 bg-transparent px-0 text-sm shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
                />
              </div>
              <Button type="submit" disabled={!hasText} aria-label="Add task" title="Add task" className="h-12 w-12 shrink-0 p-0 shadow-sm disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100">
                <Plus className="h-5 w-5" />
              </Button>
            </div>
          </div>
        </form>
      </div>
      {/* Outside the bar: its backdrop-filter would contain a fixed modal. */}
      {picker && (
        <Modal
          open
          onClose={closePicker}
          title={PICKER_TITLE[picker]}
          leftAction={
            picker === "due_date" && dateStep === "time" ? (
              <button
                type="button"
                aria-label="Edit date"
                onClick={() => setDateStep("date")}
                className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              >
                <ArrowLeft className="h-4 w-4" />
              </button>
            ) : undefined
          }
        >
          {picker === "due_date" && (
            <DatePicker
              value={draft.due_date}
              onChange={(due_date) => setDraftField({ due_date })}
              step={dateStep}
              onStepChange={setDateStep}
            />
          )}
          {picker === "label" && (
            <LabelPicker
              value={draft.label ?? ""}
              onChange={(name) => {
                setDraftField({ label: name || null });
                closePicker();
              }}
              labels={labels}
              defaultOpen
            />
          )}
          {picker === "estimation" && (
            <EstimationPicker
              value={draft.estimation}
              onChange={(estimation) => setDraftField({ estimation })}
            />
          )}
          {(picker === "estimation" || (picker === "due_date" && dateStep === "time")) && (
            <Button type="button" className="mt-4 w-full" onClick={closePicker}>
              Done
            </Button>
          )}
        </Modal>
      )}
      {loggingWork && (
        <LogWorkModal
          title={value.trim()}
          label={draft.label}
          onClose={() => setLoggingWork(false)}
          onLogged={() => {
            setLoggingWork(false);
            clearInput();
            void Promise.resolve(onCreated()).catch(() => {});
          }}
        />
      )}
    </>
  );
}

// Unset: a dashed invitation that opens the picker. Set: the value, which
// reopens the picker, plus an X that clears it.
function DraftPill({
  name,
  icon,
  text,
  className,
  style,
  onOpen,
  onClear,
}: {
  name: string;
  icon: ReactNode;
  text: string | null;
  className: string;
  style?: CSSProperties;
  onOpen: () => void;
  onClear: () => void;
}) {
  if (!text) {
    return (
      <button
        type="button"
        onClick={onOpen}
        className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-full border border-dashed border-input bg-background px-3 text-xs font-medium text-muted-foreground shadow-sm transition-colors hover:bg-accent hover:text-foreground"
      >
        {icon}
        {name}
      </button>
    );
  }
  return (
    <span
      className={cn("inline-flex h-8 shrink-0 items-center rounded-full text-xs font-medium shadow-sm", className)}
      style={style}
    >
      <button
        type="button"
        onClick={onOpen}
        title={`Change ${name.toLowerCase()}`}
        className="inline-flex h-full items-center gap-1.5 rounded-l-full pl-3 pr-1"
      >
        {icon}
        {text}
      </button>
      <button
        type="button"
        onClick={onClear}
        aria-label={`Clear ${name.toLowerCase()}`}
        title={`Clear ${name.toLowerCase()}`}
        className="inline-flex h-full items-center rounded-r-full pl-1 pr-2.5 opacity-70 transition-opacity hover:opacity-100"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </span>
  );
}
