// Client for the external kotx coding-agent API, proxied by the backend
// under /kotx (see backend app.api.kotx). kotx errors come back as
// `{ "error": "…" }`; list/detail are JSON, the …/task and …/review
// endpoints return raw markdown.

export type KotxState =
  | "drafting"
  | "draft"
  | "queued"
  | "running"
  | "awaiting_approval"
  | "awaiting_external"
  | "done"
  | "failed"
  | "cancelled"
  | "timed_out"
  | "discarded";

export const TERMINAL_STATES: ReadonlySet<KotxState> = new Set([
  "done",
  "failed",
  "cancelled",
  "timed_out",
  "discarded",
]);

export interface KotxTask {
  id: number;
  repo: string;
  title?: string | null;
  subjectType: "issue" | "pull_request";
  subjectNumber: number;
  kind: "implement" | "resolve_conflict" | "review";
  role: "assignee" | "reviewer";
  state: KotxState;
  status: string;
  stateReason: string | null;
  branch: string | null;
  prNumber: number | null;
  trackedPrNumber: number | null;
  assigned?: string[];
  assignees?: string[];
  triggeredBy: string | null;
  // Human-readable why the task appeared/refreshed ("review requested", "new
  // comment on PR", "manual run", …). Displayed verbatim.
  triggerReason: string | null;
  outcome: string | null;
  attempt: number;
  startedAt: string | null;
  finishedAt: string | null;
  createdAt: string;
  updatedAt: string;
  githubUrl: string;
  canStart: boolean;
  canApprove: boolean;
  canMerge: boolean;
  canComment: boolean;
  // What POST …/approve does when canApprove: "review" submits an approving PR
  // review (REVIEW.md as body), "pr" opens the proposed PR, and "merge"
  // merges the tracked PR. Prefer POST …/merge for the explicit merge action.
  proposes: "review" | "pr" | "merge" | null;
  canDiscard: boolean;
}

// The proposed pull request for an implement task, before it's opened. Editable
// while the task is awaiting approval; kotx uses these as the PR title and body.
export interface KotxPr {
  title: string;
  body: string;
}

export interface KotxMergeContext {
  prNumber: number | null;
  approvedBy: string | null;
  reviewUrl: string | null;
  commentMarkdown: string | null;
}

export interface KotxRunRequest {
  repo?: string;
  title?: string;
  body?: string;
  type?: "implement" | "review" | "resolve";
  number?: number;
}

class KotxError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function jsonRequest<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(`/kotx${path}`, opts);
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const msg =
      body && typeof body === "object" && "error" in body
        ? String((body as { error: unknown }).error)
        : res.statusText;
    throw new KotxError(res.status, msg);
  }
  return body as T;
}

// Like jsonRequest but returns null on 404 (the resource isn't available yet)
// rather than throwing.
async function jsonRequestOrNull<T>(path: string, opts: RequestInit = {}): Promise<T | null> {
  const res = await fetch(`/kotx${path}`, opts);
  if (res.status === 404) return null;
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const msg =
      body && typeof body === "object" && "error" in body
        ? String((body as { error: unknown }).error)
        : res.statusText;
    throw new KotxError(res.status, msg);
  }
  return body as T;
}

// Returns null on 404 — the brief/review isn't drafted yet.
async function markdownRequest(path: string): Promise<string | null> {
  const res = await fetch(`/kotx${path}`);
  if (res.status === 404) return null;
  const text = await res.text();
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object" && "error" in parsed) msg = String(parsed.error);
    } catch {
      /* keep statusText */
    }
    throw new KotxError(res.status, msg);
  }
  return text;
}

function putMarkdown(path: string, content: string) {
  return jsonRequest<{ ok: true }>(path, {
    method: "PUT",
    headers: { "content-type": "text/markdown" },
    body: content,
  });
}

export const kotx = {
  listTasks: (scope: "active" | "all" = "active") =>
    jsonRequest<KotxTask[]>(`/tasks?scope=${scope}`),
  getTask: (id: number) => jsonRequest<KotxTask>(`/tasks/${id}`),

  getBrief: (id: number) => markdownRequest(`/tasks/${id}/task`),
  putBrief: (id: number, content: string) => putMarkdown(`/tasks/${id}/task`, content),
  getReview: (id: number) => markdownRequest(`/tasks/${id}/review`),
  putReview: (id: number, content: string) => putMarkdown(`/tasks/${id}/review`, content),
  getMergeContext: (id: number) =>
    jsonRequestOrNull<KotxMergeContext>(`/tasks/${id}/merge/context`),
  // The proposed PR title + body for an implement task. null until proposed.
  getPr: (id: number) => jsonRequestOrNull<KotxPr>(`/tasks/${id}/pr`),
  putPr: (id: number, pr: KotxPr) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/pr`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(pr),
    }),

  start: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/start`, { method: "POST" }),
  // approve: for review tasks submits an approving PR review (REVIEW.md as
  // body); for implement tasks opens the proposed PR.
  approve: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/approve`, { method: "POST" }),
  merge: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/merge`, { method: "POST" }),
  // comment (review tasks only): post REVIEW.md as a plain PR comment.
  comment: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/comment`, { method: "POST" }),
  discard: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/discard`, { method: "POST" }),
  createRun: (payload: KotxRunRequest) =>
    jsonRequest<unknown>(`/runs`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    }),
};
