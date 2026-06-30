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
  | "timed_out";

export interface KotxTask {
  id: number;
  repo: string;
  subjectType: "issue" | "pull_request";
  subjectNumber: number;
  kind: "implement" | "resolve_conflict" | "review";
  role: "assignee" | "reviewer";
  state: KotxState;
  status: string;
  branch: string | null;
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
  canStop: boolean;
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

// Returns null on 404 — the brief / review isn't generated yet.
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

export const kotx = {
  listTasks: (scope: "active" | "all" = "active") =>
    jsonRequest<KotxTask[]>(`/tasks?scope=${scope}`),
  getTask: (id: number) => jsonRequest<KotxTask>(`/tasks/${id}`),

  getBrief: (id: number) => markdownRequest(`/tasks/${id}/task`),
  putBrief: (id: number, content: string) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/task`, {
      method: "PUT",
      headers: { "content-type": "text/markdown" },
      body: content,
    }),
  getReview: (id: number) => markdownRequest(`/tasks/${id}/review`),

  start: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/start`, { method: "POST" }),
  approve: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/approve`, { method: "POST" }),
  stop: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/stop`, { method: "POST" }),

  listContainers: () => jsonRequest<KotxContainer[]>(`/containers`),
};
