import { Badge } from "@/components/ui/badge";
import { runStatusLabel } from "@/components/runs/runLabels";
import { inboxBadge } from "@/lib/inbox";
import type { KotxState, KotxTask } from "@/lib/kotx";
import { cn } from "@/lib/utils";
import type { RawInput } from "@/lib/types";

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

function statusClass(label: string, state: string, subjectType: string): string {
  const direct = STATUS_CLASS[label];
  if (direct) return direct;
  if (state === "awaiting_external") {
    return subjectType === "pull_request"
      ? IN_REVIEW_CLASS // PR exists
      : WAITING_ON_PR_CLASS; // pushed, no PR yet
  }
  return STATE_CLASS[state as Exclude<KotxState, "awaiting_external">] ?? "";
}

export function RunStatusBadge({
  task,
  className,
}: {
  task: KotxTask;
  className?: string;
}) {
  const label = runStatusLabel(task);
  return (
    <Badge
      className={cn(
        statusClass(normalizeStatus(label), task.state, task.subjectType),
        className,
      )}
    >
      {label}
    </Badge>
  );
}

// Inbox badge for a raw input: kotx transitions show the run state they
// carried (the pre-consolidation runs-tab badges) instead of the storage
// status ("duplicate"), which says nothing about what kotx did. Everything
// else keeps the classic outcome badge.
export function InputStatusBadge({ input }: { input: RawInput }) {
  if (input.source === "kotx") {
    const meta = input.source_metadata ?? {};
    const state = typeof meta.kotx_state === "string" ? meta.kotx_state : "";
    const status = typeof meta.kotx_status === "string" ? meta.kotx_status : "";
    const label = normalizeStatus(status || state);
    if (label) {
      const subjectType = typeof meta.subject_type === "string" ? meta.subject_type : "";
      return <Badge className={statusClass(label, state, subjectType)}>{label}</Badge>;
    }
  }
  const kind = inboxBadge(input);
  return <Badge variant={kind}>{kind}</Badge>;
}
