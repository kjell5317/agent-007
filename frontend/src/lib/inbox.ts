import { TERMINAL_STATES, type KotxState } from "@/lib/kotx";
import type { RawInput } from "@/lib/types";

export type BadgeKind =
  | "open"
  | "not_task"
  | "duplicate"
  | "closed"
  | "reopened"
  | "updated"
  | "no_change";

// The agent outcomes on a follow-up that mean it deliberately acted on / judged
// an existing task (as opposed to the embedding auto-decider, which is a
// similarity guess). Each gets its own badge and suppresses "Make a task".
const AGENT_TASK_OUTCOMES = new Set([
  "reopened",
  "updated",
  "closed",
  "no_change",
]);

// True when the agent (not the embedding auto-decider) acted on an existing
// task from this follow-up. `auto_decided` marks the embedding path, which we
// treat as a plain duplicate the user may still override.
export function isAgentTaskFollowup(data: RawInput): boolean {
  const trace = data.agent_trace;
  if (data.status !== "duplicate" || trace?.auto_decided) return false;
  return AGENT_TASK_OUTCOMES.has(trace?.outcome ?? "");
}

// A follow-up input keeps status="duplicate". When the *agent* acted on its
// task, surface that outcome as the badge; the embedding auto-decider's links
// (and anything else) read as a plain "duplicate".
export function inboxBadge(data: RawInput): BadgeKind {
  if (isAgentTaskFollowup(data)) return data.agent_trace!.outcome as BadgeKind;
  return data.status as BadgeKind;
}

// Human-readable sender. Gmail's `from` is "Name <email>" → the name (or the
// address when unnamed); Slack's is already "display (workspace)"; kotx
// transitions come from their repo. Falls back to a source label when
// there's no `from` (e.g. manual entries).
export function senderName(data: RawInput): string {
  const raw = data.source_metadata?.from;
  const from = typeof raw === "string" ? raw.trim() : "";
  if (from) {
    const m = from.match(/^"?([^"<]*?)"?\s*<([^>]+)>$/);
    const name = m ? m[1].trim() || m[2].trim() : from;
    // Drop a trailing parenthetical (e.g. Slack's "(workspace)" suffix).
    return name.replace(/\s*\([^)]*\)\s*$/, "").trim() || name;
  }
  if (data.source === "kotx") {
    const repo = data.source_metadata?.repo;
    if (typeof repo === "string" && repo) return repo;
  }
  return data.source === "manual" ? "Manual" : data.source;
}

// A kotx transition carrying a run id. Its inbox card is a run breadcrumb, not
// a task — informational ones (drafting/queued/running) never get a task_id.
export function isKotxRun(r: RawInput): boolean {
  return r.source === "kotx" && r.source_metadata?.kotx_task_id != null;
}

// A kotx run still in flight (state not terminal) — the inbox offers
// "Dismiss run" (discard upstream) instead of "Make a task". Already-terminal
// runs (done/cancelled/discarded/…) can't be discarded, so no action.
export function isDismissibleKotxRun(r: RawInput): boolean {
  if (!isKotxRun(r)) return false;
  const state = r.source_metadata?.kotx_state;
  return typeof state === "string" && !TERMINAL_STATES.has(state as KotxState);
}

// The transition to badge a group with while kotx is working: each run's
// current state is its newest transition (earlier drafting/queued/running
// members are stale history), and the most recently active in-flight run
// wins. Null when no run is in flight. Expects members newest-first.
export function activeKotxRun(members: RawInput[]): RawInput | null {
  const latestPerRun = new Map<unknown, RawInput>();
  for (const m of members) {
    if (!isKotxRun(m)) continue;
    const runId = m.source_metadata?.kotx_task_id;
    if (!latestPerRun.has(runId)) latestPerRun.set(runId, m);
  }
  for (const input of latestPerRun.values()) {
    if (isDismissibleKotxRun(input)) return input;
  }
  return null;
}

// kotx subjects are "{repo}#{n} {title}"; drop the repo — it's shown as the
// sender and carried by the label — so the inbox card matches the task title,
// which the runner stores repo-stripped (see agent.kotx `_create_task_from_brief`).
function displaySubject(data: RawInput): string {
  const subject =
    typeof data.source_metadata?.subject === "string"
      ? data.source_metadata.subject
      : "";
  if (!subject || data.source !== "kotx") return subject;
  const repo = data.source_metadata?.repo;
  return typeof repo === "string" && repo && subject.startsWith(repo)
    ? subject.slice(repo.length)
    : subject;
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
    displaySubject(data) ||
    (data.content || "").slice(0, 80) ||
    "(no subject)"
  );
}

// Inputs that resolve to the same task belong together — so every follow-up
// and duplicate (including cross-thread, embedding-matched ones) folds in with
// its anchor. A shared task wins; then a source thread groups pre-task inputs;
// else the row stands alone. Mirrors `_GROUPED_INPUT_IDS_SQL` on the backend.
export function inputGroupKey(r: RawInput): string {
  if (r.task_id) return `task:${r.task_id}`;
  const threadId = r.source_metadata?.thread_id;
  if (typeof threadId === "string" && threadId) {
    // github:* thread keys are cross-source (gmail + kotx share them).
    return threadId.startsWith("github:")
      ? `thread:${threadId}`
      : `${r.source}:thread:${threadId}`;
  }
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

// The github threads an input references: its own thread key, plus — for kotx
// runs — the PR it works on (`pr_number`). An implement run anchors on the
// issue thread while its resolve-conflict runs anchor on the PR thread; the
// pr_number metadata is what ties the two together.
function githubRefs(r: RawInput): string[] {
  const meta = r.source_metadata ?? {};
  const refs: string[] = [];
  const threadId = meta.thread_id;
  if (typeof threadId === "string" && threadId.startsWith("github:")) {
    refs.push(threadId);
  }
  if (r.source === "kotx") {
    const repo = meta.repo;
    const pr = meta.pr_number;
    if (typeof repo === "string" && repo && pr != null) {
      refs.push(`github:${repo}#${pr}`);
    }
  }
  return refs;
}

// Fold task-less kotx-run buckets (a resolve-conflict run on the PR, or
// not-yet-backfilled preparation transitions) into the task bucket that
// references the same github subject, so a run serving a task never shows up
// as its own inbox card.
function mergeKotxRunBuckets(buckets: Map<string, RawInput[]>) {
  const taskBucketByRef = new Map<string, string>();
  for (const [key, rows] of buckets) {
    if (!key.startsWith("task:")) continue;
    for (const row of rows) {
      for (const ref of githubRefs(row)) {
        if (!taskBucketByRef.has(ref)) taskBucketByRef.set(ref, key);
      }
    }
  }

  for (const [key, rows] of buckets) {
    if (key.startsWith("task:") || !rows.every(isKotxRun)) continue;
    const target = rows
      .flatMap(githubRefs)
      .map((ref) => taskBucketByRef.get(ref))
      .find((k) => k != null);
    if (!target) continue;
    buckets.get(target)!.push(...rows);
    buckets.delete(key);
  }
}

export function groupInputs(inputs: RawInput[]): InboxGroup[] {
  const buckets = new Map<string, RawInput[]>();
  for (const r of inputs) {
    const key = inputGroupKey(r);
    const bucket = buckets.get(key);
    if (bucket) bucket.push(r);
    else buckets.set(key, [r]);
  }

  mergeKotxRunBuckets(buckets);

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
