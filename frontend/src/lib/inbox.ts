import type { RawInput } from "@/lib/types";

export type BadgeKind = "open" | "not_task" | "duplicate" | "no_change" | "closed";

export function inboxBadge(data: RawInput): BadgeKind {
  if (data.agent_trace?.outcome === "no_change") return "no_change";
  return data.status as BadgeKind;
}

// Human-readable sender. Gmail's `from` is "Name <email>" → the name (or the
// address when unnamed); Slack's is already "display (workspace)". Falls back
// to a source label when there's no `from` (e.g. manual entries).
export function senderName(data: RawInput): string {
  const raw = data.source_metadata?.from;
  const from = typeof raw === "string" ? raw.trim() : "";
  if (from) {
    const m = from.match(/^"?([^"<]*?)"?\s*<([^>]+)>$/);
    if (m) return m[1].trim() || m[2].trim();
    return from;
  }
  return data.source === "manual" ? "Manual" : data.source;
}

// The card/group headline: prefer a live (open) or completed (closed) task's
// title — that's the human-meaningful name — else the raw envelope.
export function inputTitle(data: RawInput): string {
  const linked =
    data.task_title && (data.status === "open" || data.status === "closed")
      ? data.task_title
      : null;
  return (
    linked ||
    (typeof data.source_metadata?.subject === "string"
      ? data.source_metadata.subject
      : "") ||
    (data.content || "").slice(0, 80) ||
    "(no subject)"
  );
}

// Inputs that resolve to the same task — or the same source thread — belong
// together. thread_id is preferred because it's stable across promotion
// (task_id only appears once a task exists); manual follow-ups with no thread
// fall back to task_id. Everything else is its own singleton.
export function inputGroupKey(r: RawInput): string {
  const threadId = r.source_metadata?.thread_id;
  if (typeof threadId === "string" && threadId) {
    return `${r.source}:thread:${threadId}`;
  }
  if (r.task_id) return `task:${r.task_id}`;
  return `input:${r.id}`;
}

export interface InboxGroup {
  key: string;
  members: RawInput[]; // newest-first
  newest: RawInput;
  sort: string; // newest received_at, for ordering groups
  title: string;
  /** Member anchoring a still-open task, if any. */
  liveTask: RawInput | null;
  /** Member anchoring a completed task, if any. */
  closedTask: RawInput | null;
}

function byReceivedDesc(a: RawInput, b: RawInput): number {
  return new Date(b.received_at).getTime() - new Date(a.received_at).getTime();
}

export function groupInputs(inputs: RawInput[]): InboxGroup[] {
  const buckets = new Map<string, RawInput[]>();
  for (const r of inputs) {
    const key = inputGroupKey(r);
    const bucket = buckets.get(key);
    if (bucket) bucket.push(r);
    else buckets.set(key, [r]);
  }

  const groups: InboxGroup[] = [];
  for (const [key, rows] of buckets) {
    const members = [...rows].sort(byReceivedDesc);
    const newest = members[0];
    const liveTask =
      members.find((m) => m.status === "open" && m.task_id) ?? null;
    const closedTask =
      members.find((m) => m.status === "closed" && m.task_id) ?? null;
    const rep = liveTask ?? closedTask ?? newest;
    groups.push({
      key,
      members,
      newest,
      sort: newest.received_at,
      title: inputTitle(rep),
      liveTask,
      closedTask,
    });
  }

  return groups.sort(
    (a, b) => new Date(b.sort).getTime() - new Date(a.sort).getTime(),
  );
}
