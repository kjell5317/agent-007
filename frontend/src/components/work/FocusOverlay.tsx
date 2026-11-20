import { useEffect } from "react";
import { Check, Coffee, Play, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PAUSE_REASON_ICON } from "@/components/work/PauseChooser";
import { useWorkActions } from "@/components/work/useWorkActions";
import { takeBreak } from "@/hooks/useFocusOverlay";
import { useNow } from "@/hooks/useNow";
import { cn } from "@/lib/utils";
import { elapsedMs, fmtElapsed, workedMinutes } from "@/lib/work";
import type { PauseReason, Task } from "@/lib/types";

const BUTTON_CLASS = "h-14 min-w-0 text-base";

// The running task and nothing else: the clock only counts up, and every
// button acts at once. A break taken here keeps it up, with the clock held at
// the time worked so far.
export function FocusOverlay({
  task,
  breakSince,
  onChanged,
  onClose,
  onOpen,
}: {
  task: Task;
  breakSince: string | null;
  onChanged: () => Promise<void> | void;
  onClose: () => void;
  onOpen: (id: string) => void;
}) {
  const now = useNow(1_000);
  const work = useWorkActions(task, { onChanged });

  // Capture phase, so Escape closes only this and not a task modal beneath.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const pauseButton = (reason: PauseReason, label: string, className: string, variant: "secondary" | "outline") => {
    const Icon = PAUSE_REASON_ICON[reason];
    return (
      <Button
        variant={variant}
        className={cn(BUTTON_CLASS, className)}
        onClick={() => {
          if (reason === "break") takeBreak(task.id);
          work.pauseFor(reason);
        }}
      >
        <Icon className="h-5 w-5 shrink-0" />
        <span className="truncate">{label}</span>
      </Button>
    );
  };

  return (
    // touch-none keeps iOS from scrolling the page behind.
    <div
      role="dialog"
      aria-modal="true"
      aria-label={breakSince ? `On a break from ${task.title}` : `Working on ${task.title}`}
      className="fixed inset-0 z-[60] flex touch-none flex-col bg-background pb-[env(safe-area-inset-bottom)] pl-[env(safe-area-inset-left)] pr-[env(safe-area-inset-right)] pt-[env(safe-area-inset-top)] text-foreground duration-150 animate-in fade-in-0 motion-reduce:animate-none"
    >
      <div className="flex justify-end p-2">
        <button
          type="button"
          aria-label="Close"
          title="Close"
          onClick={onClose}
          className="inline-flex h-12 w-12 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <X className="h-6 w-6" />
        </button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-6 px-6 text-center">
        <button
          type="button"
          onClick={() => onOpen(task.id)}
          className="max-w-xl text-2xl font-semibold leading-tight"
        >
          <span className="line-clamp-4 break-words">{task.title}</span>
        </button>
        <div
          role="timer"
          aria-label="Time worked on this task"
          className={cn("text-7xl font-semibold tabular-nums sm:text-8xl", breakSince && "text-muted-foreground")}
        >
          {fmtElapsed(workedMinutes(task, now) * 60_000)}
        </div>
        {breakSince && (
          <div className="flex items-center gap-2 text-lg text-muted-foreground">
            <Coffee className="h-5 w-5 shrink-0" />
            <span>
              On break <span className="tabular-nums">{fmtElapsed(elapsedMs(breakSince, now))}</span>
            </span>
          </div>
        )}
      </div>
      {breakSince ? (
        <div className="mx-auto grid w-full max-w-md grid-cols-2 gap-3 p-4">
          <Button className={cn(BUTTON_CLASS, "col-span-2")} onClick={work.start}>
            <Play className="h-5 w-5 shrink-0" />
            Resume
          </Button>
          <Button
            variant="secondary"
            className={cn(BUTTON_CLASS, "col-span-2")}
            onClick={() => {
              onClose();
              work.complete();
            }}
          >
            <Check className="h-5 w-5 shrink-0" />
            Done
          </Button>
        </div>
      ) : (
        <div className="mx-auto grid w-full max-w-md grid-cols-2 gap-3 p-4">
          {pauseButton("break", "Break", "col-span-2", "secondary")}
          <Button className={cn(BUTTON_CLASS, "col-span-2")} onClick={work.complete}>
            <Check className="h-5 w-5 shrink-0" />
            Done
          </Button>
          {pauseButton("interrupted", "Interrupted", "", "outline")}
          {pauseButton("waiting", "Waiting", "", "outline")}
        </div>
      )}
    </div>
  );
}
