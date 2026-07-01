import { useState } from "react";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { runTitle } from "@/components/runs/runLabels";
import { kotx, type KotxState, type KotxTask } from "@/lib/kotx";

interface Props {
  task: KotxTask;
  onChanged: () => Promise<void> | void;
  onOpen: (id: number) => void;
}

// Fallback colors for unknown upstream status labels.
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

const IN_REVIEW_CLASS = "bg-violet-500 text-white dark:bg-violet-500/25 dark:text-violet-200";
const WAITING_ON_PR_CLASS =
  "bg-violet-400 text-slate-900 dark:bg-violet-500/20 dark:text-violet-200";

// One distinct color per displayed run status. The key is the text shown in
// the badge, not the workflow state that happened to produce it.
const STATUS_CLASS: Record<string, string> = {
  "preparing task": STATE_CLASS.drafting,
  drafting: STATE_CLASS.drafting,
  draft: STATE_CLASS.draft,
  "waiting on my approval": STATE_CLASS.awaiting_approval,
  "awaiting approval": STATE_CLASS.awaiting_approval,
  queued: STATE_CLASS.queued,
  running: STATE_CLASS.running,
  "in review": IN_REVIEW_CLASS,
  "waiting on pr": WAITING_ON_PR_CLASS,
  "awaiting external": IN_REVIEW_CLASS,
  done: STATE_CLASS.done,
  failed: STATE_CLASS.failed,
  cancelled: STATE_CLASS.cancelled,
  canceled: STATE_CLASS.cancelled,
  "timed out": STATE_CLASS.timed_out,
  discarded: STATE_CLASS.discarded,
};

function normalizeStatus(status: string): string {
  return status
    .trim()
    .toLowerCase()
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ");
}

function fallbackStatusClass(task: KotxTask): string {
  if (task.state === "awaiting_external") {
    return task.subjectType === "pull_request"
      ? IN_REVIEW_CLASS // PR exists
      : WAITING_ON_PR_CLASS; // pushed, no PR yet
  }
  return STATE_CLASS[task.state];
}

function statusClass(task: KotxTask): string {
  return STATUS_CLASS[normalizeStatus(task.status)] ?? fallbackStatusClass(task);
}

// The label of the modal's primary action — the card leads with the same word,
// but tapping it opens the modal rather than acting directly.
function actionHint(task: KotxTask): string | null {
  if (task.canStart) return "Start";
  if (task.canComment) return "Comment";
  if (task.canApprove) return task.proposes === "pr" ? "Open PR" : "Approve";
  return null;
}

export function RunCard({ task, onChanged, onOpen }: Props) {
  const [busy, setBusy] = useState(false);

  const title = runTitle(task);
  const hint = actionHint(task);

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

  const discard = runAction(() => kotx.discard(task.id), "Discarded");

  return (
    <Card
      role="button"
      tabIndex={0}
      onClick={() => onOpen(task.id)}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(task.id);
        }
      }}
      className="cursor-pointer transition-colors hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <CardContent className="flex items-center gap-2 p-3">
        {task.canDiscard ? (
          <IconAction onClick={discard} disabled={busy} title="Discard task">
            <Trash2 className="h-5 w-5" />
          </IconAction>
        ) : (
          // Keep the leading column reserved so cards align whether or not
          // they carry a discard button.
          <div className="h-8 w-8 shrink-0" />
        )}

        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <div className="min-w-0 truncate font-medium leading-snug" title={title}>
            {title}
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <Badge className={statusClass(task)}>{task.status}</Badge>
            <span className="min-w-0 flex-1 truncate" title={task.repo}>
              {task.repo}
            </span>
          </div>
        </div>

        {hint && (
          <Button
            size="sm"
            className="shrink-0"
            onClick={(e) => {
              e.stopPropagation();
              onOpen(task.id);
            }}
            disabled={busy}
          >
            {hint}
          </Button>
        )}
      </CardContent>
    </Card>
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
