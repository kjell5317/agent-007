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
  triggeredBy: string | null;
  outcome: string | null;
  attempt: number;
  startedAt: string | null;
  finishedAt: string | null;
  createdAt: string;
  updatedAt: string;
  githubUrl: string;
  canStart: boolean;
  canApprove: boolean;
  canComment: boolean;
  // What POST …/approve does when canApprove: "review" submits an approving PR
  // review (REVIEW.md as body), "pr" opens the proposed PR.
  proposes: "review" | "pr" | null;
  canDiscard: boolean;
}

// The proposed pull request for an implement task, before it's opened. Editable
// while the task is awaiting approval; kotx uses these as the PR title and body.
export interface KotxPr {
  title: string;
  body: string;
}

export interface KotxContainer {
  id: string;
  name: string;
  runId: number;
  image: string;
  state: string;
  status: string;
  createdAt: number;
}

export interface KotxLogEntry {
  offset: number;
  record?: unknown;
  raw?: string;
  parseError?: string;
}

export interface KotxLogPage {
  text: string | null;
  hasMoreBefore: boolean;
  // Byte cursor to pass as `before` when paging older; null when there's no
  // older page.
  before: number | null;
}

export interface KotxLogParams {
  limit?: number;
  before?: number;
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

// Returns null on 404 — the document isn't generated yet (brief/review not
// drafted, or prompt/log absent until a run has started).
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

// kotx returns the log as a JSON page: `{ runId, entries, page }`, where each
// entry is `{ offset, record }` (parsed JSONL) or `{ offset, raw, parseError }`
// (a line that wasn't valid JSON). Flatten entries back to one line each — a
// stringified record or the raw text — and let the caller pretty-print them.
function entryToLine(entry: KotxLogEntry): string {
  if (typeof entry.raw === "string") return entry.raw;
  if ("record" in entry) return JSON.stringify(entry.record);
  return "";
}

interface LogPageResponse {
  entries?: KotxLogEntry[];
  page?: {
    hasMoreBefore?: boolean;
    nextBefore?: number | null;
  };
}

async function logRequest(path: string, params: KotxLogParams = {}): Promise<KotxLogPage> {
  const qs = new URLSearchParams();
  if (params.limit !== undefined) qs.set("limit", String(params.limit));
  if (params.before !== undefined) qs.set("before", String(params.before));

  const query = qs.toString();
  const res = await fetch(`/kotx${path}${query ? `?${query}` : ""}`);
  if (res.status === 404) return { text: null, hasMoreBefore: false, before: null };
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

  const body = JSON.parse(text) as LogPageResponse;
  const entries = body.entries ?? [];
  return {
    text: entries.map(entryToLine).join("\n"),
    hasMoreBefore: body.page?.hasMoreBefore ?? false,
    before: body.page?.nextBefore ?? null,
  };
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
  getPrompt: (id: number) => markdownRequest(`/tasks/${id}/prompt`),
  // The proposed PR title + body for an implement task. null until proposed.
  getPr: (id: number) => jsonRequestOrNull<KotxPr>(`/tasks/${id}/pr`),
  putPr: (id: number, pr: KotxPr) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/pr`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(pr),
    }),
  getLog: (id: number, params: KotxLogParams = {}) =>
    logRequest(`/tasks/${id}/log`, params),

  start: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/start`, { method: "POST" }),
  // approve: for review tasks submits an approving PR review (REVIEW.md as
  // body); for implement tasks opens the proposed PR.
  approve: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/approve`, { method: "POST" }),
  // comment (review tasks only): post REVIEW.md as a plain PR comment.
  comment: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/comment`, { method: "POST" }),
  discard: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/discard`, { method: "POST" }),

  listContainers: () => jsonRequest<KotxContainer[]>(`/containers`),
};
