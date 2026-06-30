import { useState } from "react";
import { ChevronRight, Square, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { RunDocModal } from "@/components/runs/RunDocModal";
import { kotx, TERMINAL_STATES, type KotxState, type KotxTask } from "@/lib/kotx";
import { cn } from "@/lib/utils";

interface Props {
  task: KotxTask;
  onChanged: () => Promise<void> | void;
}

// One distinct color per run status. awaiting_external is split below by
// subjectType (a PR subject means a PR exists → "in review"; otherwise the
// branch is pushed but no PR is open yet → "waiting on PR").
const STATE_CLASS: Record<Exclude<KotxState, "awaiting_external">, string> = {
  drafting: "bg-slate-500 text-white dark:bg-slate-600 dark:text-slate-100", // preparing task
  draft: "bg-amber-500 text-slate-900 dark:bg-amber-500/25 dark:text-amber-200", // waiting on my approval
  queued: "bg-slate-400 text-slate-900 dark:bg-slate-500/35 dark:text-slate-100",
  running: "bg-blue-500 text-white dark:bg-blue-500/25 dark:text-blue-200",
  awaiting_approval: "bg-amber-500 text-slate-900 dark:bg-amber-500/25 dark:text-amber-200", // waiting on my approval
  done: "bg-emerald-500 text-white dark:bg-emerald-500/25 dark:text-emerald-200",
  failed: "bg-red-500 text-white dark:bg-red-500/25 dark:text-red-200",
  cancelled: "bg-zinc-500 text-white dark:bg-zinc-500/30 dark:text-zinc-200",
  timed_out: "bg-red-600 text-white dark:bg-red-600/30 dark:text-red-100",
  discarded: "bg-zinc-400 text-slate-900 dark:bg-zinc-500/25 dark:text-zinc-200",
};

function statusClass(task: KotxTask): string {
  if (task.state === "awaiting_external") {
    return task.subjectType === "pull_request"
      ? "bg-violet-500 text-white dark:bg-violet-500/25 dark:text-violet-200" // in review — PR exists
      : "bg-violet-400 text-slate-900 dark:bg-violet-500/20 dark:text-violet-200"; // waiting on PR — pushed, no PR yet
  }
  return STATE_CLASS[task.state];
}

export function RunCard({ task, onChanged }: Props) {
  // review-kind runs surface REVIEW.md; everything else surfaces TASK.md.
  const doc: "task" | "review" = task.kind === "review" ? "review" : "task";
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const subjectLabel = task.subjectType === "pull_request" ? "PR" : "Issue";
  const actionable = task.canStart || task.canApprove;
  const hint = task.canStart ? "Start" : task.canApprove ? "Comment" : "Open";
  const terminal = TERMINAL_STATES.has(task.state);

  const runAction = (fn: () => Promise<unknown>, msg: string) => async (e: React.MouseEvent) => {
    e.stopPropagation();
    setBusy(true);
    try {
      await fn();
      toast.success(msg);
      await onChanged();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const stop = runAction(() => kotx.stop(task.id), "Stopping run");
  const discard = runAction(() => kotx.discard(task.id), "Discarded");

  return (
    <>
    <Card
      role="button"
      tabIndex={0}
      onClick={() => setOpen(true)}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          setOpen(true);
        }
      }}
      className="cursor-pointer transition-colors hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <CardContent className="flex items-center gap-2 p-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <div
            className="min-w-0 truncate font-medium leading-snug"
            title={`${subjectLabel} #${task.subjectNumber} · ${task.repo}`}
          >
            {subjectLabel} #{task.subjectNumber}{" "}
            <span className="text-muted-foreground">{task.repo}</span>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <Badge className={statusClass(task)}>{task.status}</Badge>
            <span className="capitalize">{task.kind.replace("_", " ")}</span>
          </div>
        </div>

        {task.canStop && (
          <IconAction onClick={stop} disabled={busy} title="Stop run">
            <Square className="h-3.5 w-3.5" />
          </IconAction>
        )}
        {!terminal && (
          <IconAction onClick={discard} disabled={busy} title="Discard task">
            <Trash2 className="h-3.5 w-3.5" />
          </IconAction>
        )}
        <span
          className={cn(
            "inline-flex shrink-0 items-center gap-0.5 text-xs font-medium",
            actionable ? "text-primary" : "text-muted-foreground",
          )}
        >
          {actionable && hint}
          <ChevronRight className="h-4 w-4" />
        </span>
      </CardContent>
    </Card>
    {open && (
      <RunDocModal task={task} doc={doc} onClose={() => setOpen(false)} onChanged={onChanged} />
    )}
    </>
  );
}

function IconAction({
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-destructive disabled:pointer-events-none disabled:opacity-50"
      {...props}
    >
      {children}
    </button>
  );
}
