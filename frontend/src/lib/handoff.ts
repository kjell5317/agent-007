import type { HandoffKind, Task, TaskHandoff } from "./types";

export const HANDOFF_KIND_LABEL: Record<HandoffKind, string> = {
  web_research: "Research on the web",
  own_data_lookup: "Look up in my data",
  email_reply: "Draft a reply",
  code_change: "Open GitHub issue",
};

const RUNNING_LABEL: Record<HandoffKind, string> = {
  web_research: "Researching…",
  own_data_lookup: "Looking up…",
  email_reply: "Drafting reply…",
  code_change: "Opening issue…",
};

const READY_LABEL: Record<HandoffKind, string> = {
  web_research: "Researched",
  own_data_lookup: "Agent notes",
  email_reply: "Draft ready",
  code_change: "Issue opened",
};

export type HandoffTone = "proposed" | "running" | "ready" | "failed";

// The card chip; declined hand-offs show nothing.
export function handoffChip(handoff: TaskHandoff | null | undefined): { text: string; tone: HandoffTone } | null {
  if (!handoff || handoff.state === "declined") return null;
  if (handoff.state === "proposed") return { text: "Agent can open an issue", tone: "proposed" };
  if (handoff.state === "running") return { text: RUNNING_LABEL[handoff.kind], tone: "running" };
  if (handoff.state === "failed") return { text: "Agent failed", tone: "failed" };
  return { text: READY_LABEL[handoff.kind], tone: "ready" };
}

export function handoffActive(handoff: TaskHandoff | null | undefined): boolean {
  return handoff?.state === "proposed" || handoff?.state === "running";
}

// Kinds the "Hand off…" menu offers; the backend has the final say. A subtask
// lists its parent's inputs but has no source of its own to reply to or file.
export function manualHandoffKinds(task: Pick<Task, "raw_inputs" | "link" | "parent_task_id">): HandoffKind[] {
  const kinds: HandoffKind[] = ["web_research", "own_data_lookup"];
  if (task.parent_task_id) return kinds;
  if ((task.raw_inputs ?? []).some((input) => input.source === "gmail")) kinds.push("email_reply");
  if (!/github\.com/i.test(task.link ?? "")) kinds.push("code_change");
  return kinds;
}

export function resultText(result: Record<string, unknown> | null, key: string): string | null {
  const value = result?.[key];
  return typeof value === "string" && value ? value : null;
}

export function resultList(result: Record<string, unknown> | null, key: string): string[] {
  const value = result?.[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
