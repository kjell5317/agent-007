import { useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { CompleteLogSheet } from "@/components/work/LogWorkModal";
import { PauseChooser } from "@/components/work/PauseChooser";
import { api } from "@/lib/api";
import { toast } from "@/lib/toast";
import { needsWorkLog, runningSince } from "@/lib/work";
import type { PauseReason, Task } from "@/lib/types";

export interface WorkActions {
  busy: boolean;
  completing: boolean;
  running: boolean;
  waiting: boolean;
  start: () => void;
  pause: () => void;
  resume: () => void;
  complete: () => void;
  // The pause chooser and the log sheet. Portaled, so a card fading out while
  // it completes doesn't fade them; React events still bubble to where they
  // render, so keep them out of click-to-open areas.
  dialogs: ReactNode;
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
  const [busy, setBusy] = useState(false);
  const [completing, setCompleting] = useState(false);
  const [dialog, setDialog] = useState<"pause" | "log" | null>(null);

  const act = async (action: () => Promise<Task>, message?: string) => {
    if (busy || completing) return;
    setBusy(true);
    try {
      const saved = await action();
      onSaved?.(saved);
      if (message) toast.success(message);
      await onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // Throws, so the log sheet can show the error next to its fields.
  const close = async (action: () => Promise<unknown>) => {
    setCompleting(true);
    try {
      await action();
      setDialog(null);
      toast.success("Marked done");
      onClosed?.();
      await onChanged();
    } finally {
      setCompleting(false);
    }
  };

  const pauseFor = (reason: PauseReason) => {
    setDialog(null);
    void act(
      () => api.pauseTask(task.id, reason),
      reason === "waiting" ? "Off the calendar until you resume" : "Paused",
    );
  };

  const complete = () => {
    if (busy || completing) return;
    if (needsWorkLog(task)) {
      setDialog("log");
      return;
    }
    close(() => api.completeTask(task.id)).catch((e) => toast.error((e as Error).message));
  };

  return {
    busy: busy || completing,
    completing,
    running: runningSince(task) != null,
    waiting: task.status === "open" && task.schedule_state === "waiting",
    start: () => void act(() => api.startTask(task.id)),
    pause: () => setDialog("pause"),
    resume: () => void act(() => api.resumeTask(task.id), "Back in the planner"),
    complete,
    dialogs: dialog && createPortal(
      dialog === "pause" ? (
        <PauseChooser busy={busy} onPick={pauseFor} onClose={() => setDialog(null)} />
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
