import type { HandoffKind, Label, Task, TaskHandoff, TaskRawInput } from "./types";

export const HANDOFF_KIND_LABEL: Record<HandoffKind, string> = {
  research: "Research",
  email_reply: "Draft a reply",
  code_change: "Open GitHub issue",
};

const RUNNING_LABEL: Record<HandoffKind, string> = {
  research: "Researching…",
  email_reply: "Drafting reply…",
  code_change: "Opening issue…",
};

const READY_LABEL: Record<HandoffKind, string> = {
  research: "Researched",
  email_reply: "Draft ready",
  code_change: "Issue opened",
};

export type HandoffTone = "proposed" | "running" | "ready" | "failed";

export function latestHandoff(task: Pick<Task, "handoffs">): TaskHandoff | null {
  return task.handoffs?.[0] ?? null;
}

// The card chip; declined hand-offs show nothing.
export function handoffChip(handoff: TaskHandoff | null | undefined): { text: string; tone: HandoffTone } | null {
  if (!handoff || handoff.state === "declined") return null;
  if (handoff.state === "proposed") return { text: "Agent can open an issue", tone: "proposed" };
  if (handoff.state === "running") return { text: RUNNING_LABEL[handoff.kind], tone: "running" };
  if (handoff.state === "failed") return { text: "Agent failed", tone: "failed" };
  if (handoff.kind === "research" && handoff.result?.completed === true) return { text: "Answered", tone: "ready" };
  return { text: READY_LABEL[handoff.kind], tone: "ready" };
}

export function handoffActive(handoff: TaskHandoff | null | undefined): boolean {
  return handoff?.state === "proposed" || handoff?.state === "running";
}

// Kinds the "Hand off…" menu offers, by the same rules the backend checks. A
// subtask lists its parent's inputs but has no source of its own to reply to.
export function manualHandoffKinds(
  task: Pick<Task, "raw_inputs" | "link" | "parent_task_id" | "label">,
  labels: Pick<Label, "name" | "github_repo">[],
): HandoffKind[] {
  const kinds: HandoffKind[] = ["research"];
  if (!task.parent_task_id && (task.raw_inputs ?? []).some((input) => input.source === "gmail")) {
    kinds.push("email_reply");
  }
  const repo = labels.find((label) => label.name === task.label)?.github_repo;
  if (repo && !/github\.com/i.test(task.link ?? "")) kinds.push("code_change");
  return kinds;
}

// A finished agent run, shaped as a linked input so the task modal lists it
// with its trace next to the inputs.
export function handoffAsInput(handoff: TaskHandoff, taskId: string): TaskRawInput {
  const summary = resultText(handoff.result, "summary") ?? resultText(handoff.result, "error") ?? "";
  return {
    id: handoff.id,
    source: "agent",
    external_id: null,
    content: summary,
    source_metadata: { subject: `${HANDOFF_KIND_LABEL[handoff.kind]}: ${handoff.brief}`, from: "Agent" },
    received_at: handoff.created_at,
    processed_at: handoff.updated_at,
    status: "duplicate",
    task_id: taskId,
    task_title: null,
    agent_trace: handoff.trace,
    source_url: resultText(handoff.result, "mail_url") ?? resultText(handoff.result, "issue_url"),
  };
}

export function resultText(result: Record<string, unknown> | null, key: string): string | null {
  const value = result?.[key];
  return typeof value === "string" && value ? value : null;
}

export function resultList(result: Record<string, unknown> | null, key: string): string[] {
  const value = result?.[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
