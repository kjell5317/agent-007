import type { KotxTask } from "@/lib/kotx";

export function runKindLabel(kind: KotxTask["kind"]): string {
  return kind.replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

export function subjectLabel(task: KotxTask): string {
  const prefix =
    task.subjectType === "pull_request"
      ? `PR #${task.subjectNumber}`
      : `#${task.subjectNumber}`;
  const title = task.title?.trim();

  return title ? `${prefix} ${title}` : prefix;
}

export function runTitle(task: KotxTask): string {
  return `${runKindLabel(task.kind)} ${subjectLabel(task)}`;
}

export function isPrFollowUpRun(task: KotxTask): boolean {
  return task.subjectType === "pull_request" && task.proposes === "pr";
}

export function runStatusLabel(task: KotxTask): string {
  return isPrFollowUpRun(task) ? "in review" : task.status;
}

// The label of the modal's primary action. Run cards and grouped rows use the
// same visible hint, but the button opens the modal rather than acting directly.
export function actionHint(task: KotxTask): string | null {
  if (task.canStart) return "Start";
  if (task.canComment) return "Comment";
  if (isPrFollowUpRun(task)) return null;
  if (task.canApprove) return task.proposes === "pr" ? "Open PR" : "Approve";
  return null;
}
