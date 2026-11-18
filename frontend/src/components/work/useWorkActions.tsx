import { useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { CompleteLogSheet } from "@/components/work/LogWorkModal";
import { PauseChooser } from "@/components/work/PauseChooser";
import { showFocus } from "@/hooks/useFocusOverlay";
import { api } from "@/lib/api";
import { publishLocal } from "@/lib/events";
import { toast } from "@/lib/toast";
import { afterWork, runningSince, type WorkStep } from "@/lib/work";
import type { PauseReason, Task } from "@/lib/types";

export interface WorkActions {
  // The log sheet is closing the task.
  completing: boolean;
  running: boolean;
  waiting: boolean;
  start: () => void;
  // Opens the pause chooser.
  pause: () => void;
  pauseFor: (reason: PauseReason) => void;
  resume: () => void;
  complete: () => void;
  logComplete: () => void;
  focus: () => void;
  // The pause chooser and the log sheet. Portaled, so a card fading out while
  // it completes doesn't fade them; React events still bubble to where they
  // render, so keep them out of click-to-open areas.
  dialogs: ReactNode;
}

// Taps show at once, so their requests queue per task (a quick Start → Pause
// must reach the server in that order) and only the newest tap's answer lands.
const queues = new Map<string, Promise<unknown>>();
const newestTap = new Map<string, number>();
let taps = 0;

function send(taskId: string, call: () => Promise<Task>) {
  const tap = ++taps;
  newestTap.set(taskId, tap);
  const result = (queues.get(taskId) ?? Promise.resolve()).then(call);
  queues.set(taskId, result.catch(() => {}));
  return { result, newest: () => newestTap.get(taskId) === tap };
}

export function useWorkActions(
  task: Task,
  {
    onChanged,
    onSaved,
    onClosed,
  }: {
    onChanged: () => Promise<void> | void;
    onSaved?: (task: Task) => void;
    onClosed?: () => void;
  },
): WorkActions {
  const [completing, setCompleting] = useState(false);
  const [dialog, setDialog] = useState<"pause" | "log" | null>(null);

  const show = (next: Task) => {
    publishLocal({ type: "task", data: next });
    onSaved?.(next);
  };

  const resync = () => void Promise.resolve(onChanged()).catch(() => {});

  const act = (step: WorkStep, call: () => Promise<Task>, message?: string) => {
    const before = task;
    show(afterWork(task, step, Date.now()));
    if (step === "complete") onClosed?.();
    const { result, newest } = send(task.id, call);
    result.then(
      (saved) => {
        if (newest()) show(saved);
        if (message) toast.success(message);
        // A closed subtask also changes its parent.
        if (step === "complete") resync();
      },
      (e) => {
        if (newest()) show(before);
        toast.error((e as Error).message);
        resync();
      },
    );
  };

  // Throws, so the log sheet can show the error next to its fields.
  const close = async (call: () => Promise<Task | void>) => {
    setCompleting(true);
    try {
      const saved = await call();
      setDialog(null);
      show(saved ?? afterWork(task, "complete", Date.now()));
      toast.success("Marked done");
      onClosed?.();
      resync();
    } finally {
      setCompleting(false);
    }
  };

  const pauseFor = (reason: PauseReason) => {
    setDialog(null);
    act(
      reason,
      () => api.pauseTask(task.id, reason),
      reason === "waiting" ? "Off the calendar until you resume" : "Paused",
    );
  };

  const complete = () => {
    setDialog(null);
    act("complete", () => api.completeTask(task.id), "Marked done");
  };

  const running = runningSince(task) != null;

  return {
    completing,
    running,
    waiting: task.status === "open" && task.schedule_state === "waiting",
    start: () => {
      showFocus();
      act("start", () => api.startTask(task.id));
    },
    pause: () => setDialog("pause"),
    pauseFor,
    resume: () => act("resume", () => api.resumeTask(task.id), "Back in the planner"),
    complete,
    logComplete: () => setDialog("log"),
    focus: showFocus,
    dialogs: dialog && createPortal(
      dialog === "pause" ? (
        <PauseChooser onPick={pauseFor} onComplete={running ? complete : undefined} onClose={() => setDialog(null)} />
      ) : (
        <CompleteLogSheet
          onLog={(span) => close(() => api.logWork(task.id, { ...span, complete: true }))}
          onUnknown={() => close(() => api.closeTask(task.id))}
          onClose={() => setDialog(null)}
        />
      ),
      document.body,
    ),
  };
}
