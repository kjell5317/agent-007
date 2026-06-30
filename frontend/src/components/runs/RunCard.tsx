import { useState } from "react";
import { ExternalLink, FileText, GitBranch, Square } from "lucide-react";
import { toast } from "sonner";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { RunDocModal } from "@/components/runs/RunDocModal";
import { kotx, type KotxState, type KotxTask } from "@/lib/kotx";

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

  const stop = async () => {
    setBusy(true);
    try {
      await kotx.stop(task.id);
      toast.success("Stopping run");
      await onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const cta = task.canStart
    ? "Review & start"
    : task.canApprove
      ? "Review & approve"
      : doc === "review"
        ? "Review"
        : "Brief";

  return (
    <Card>
      <CardContent className="p-3">
        <div className="flex items-start gap-2">
          <div className="flex min-w-0 flex-1 flex-col gap-1.5">
            <div className="flex items-center gap-2">
              <a
                href={task.githubUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="min-w-0 truncate font-medium leading-snug hover:underline"
                title={`${task.repo} #${task.subjectNumber}`}
              >
                {task.repo} <span className="text-muted-foreground">#{task.subjectNumber}</span>
              </a>
              <ExternalLink className="h-3 w-3 shrink-0 text-muted-foreground" />
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <Badge variant={STATE_VARIANT[task.state]}>{task.status}</Badge>
              <span className="capitalize">{task.kind.replace("_", " ")}</span>
              {task.branch && (
                <span className="inline-flex items-center gap-1" title={task.branch}>
                  <GitBranch className="h-3 w-3" />
                  <span className="max-w-[12rem] truncate">{task.branch}</span>
                </span>
              )}
              {task.outcome && <span title="Last outcome">· {task.outcome}</span>}
            </div>
          </div>
        </div>

        <div className="mt-2.5 flex items-center gap-2">
          <Button
            variant={task.canStart || task.canApprove ? "default" : "outline"}
            size="sm"
            onClick={() => setOpen(true)}
            disabled={busy}
          >
            <FileText className="h-3.5 w-3.5" />
            {cta}
          </Button>
          {task.canStop && (
            <Button
              variant="ghost"
              size="sm"
              onClick={stop}
              disabled={busy}
              className="ml-auto text-muted-foreground hover:text-destructive"
              title="Stop run"
            >
              <Square className="h-3.5 w-3.5" />
              Stop
            </Button>
          )}
        </div>
      </CardContent>

      {open && (
        <RunDocModal task={task} doc={doc} onClose={() => setOpen(false)} onChanged={onChanged} />
      )}
    </Card>
  );
}
