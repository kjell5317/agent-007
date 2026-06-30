import { useState } from "react";
import { ChevronRight, ExternalLink, Square } from "lucide-react";
import { toast } from "sonner";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { RunDocModal } from "@/components/runs/RunDocModal";
import { kotx, type KotxState, type KotxTask } from "@/lib/kotx";
import { cn } from "@/lib/utils";

interface Props {
  task: KotxTask;
  onChanged: () => Promise<void> | void;
}

const STATE_VARIANT: Record<KotxState, BadgeProps["variant"]> = {
  drafting: "muted",
  draft: "open",
  queued: "muted",
  running: "no_change",
  awaiting_approval: "open",
  awaiting_external: "duplicate",
  done: "closed",
  failed: "not_task",
  cancelled: "closed",
  timed_out: "not_task",
};

export function RunCard({ task, onChanged }: Props) {
  // review-kind runs surface REVIEW.md; everything else surfaces TASK.md.
  const doc: "task" | "review" = task.kind === "review" ? "review" : "task";
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const subjectLabel = task.subjectType === "pull_request" ? "PR" : "Issue";
  const actionable = task.canStart || task.canApprove;
  const hint = task.canStart ? "Start" : task.canApprove ? "Approve" : "Open";

  const stop = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setBusy(true);
    try {
      await kotx.stop(task.id);
      toast.success("Stopping run");
      await onChanged();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
    <Card
      role="button"
      tabIndex={0}
      onClick={() => setOpen(true)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          setOpen(true);
        }
      }}
      className={cn(
        "cursor-pointer transition-colors hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        actionable && "ring-1 ring-primary/40",
      )}
    >
      <CardContent className="flex items-center gap-2 p-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <div className="flex items-center gap-1.5">
            <span className="min-w-0 truncate font-medium leading-snug" title={task.repo}>
              {task.repo}{" "}
              <span className="text-muted-foreground">
                {subjectLabel} #{task.subjectNumber}
              </span>
            </span>
            <a
              href={task.githubUrl}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="shrink-0 text-muted-foreground hover:text-foreground"
              title="Open on GitHub"
            >
              <ExternalLink className="h-3 w-3" />
            </a>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <Badge variant={STATE_VARIANT[task.state]}>{task.status}</Badge>
            <span className="capitalize">{task.kind.replace("_", " ")}</span>
            {task.outcome && <span title="Last outcome">· {task.outcome}</span>}
          </div>
        </div>

        {task.canStop && (
          <button
            type="button"
            onClick={stop}
            disabled={busy}
            title="Stop run"
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-destructive disabled:pointer-events-none disabled:opacity-50"
          >
            <Square className="h-3.5 w-3.5" />
          </button>
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
