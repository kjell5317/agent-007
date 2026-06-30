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
