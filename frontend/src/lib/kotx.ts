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

export interface KotxLogPage {
  text: string | null;
  hasMoreBefore: boolean;
  before: string | null;
}

export interface KotxLogParams {
  tail?: number;
  limit?: number;
  before?: string | number;
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

function parseCursor(value: unknown): string | null {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function parseBoolean(value: unknown): boolean | null {
  if (typeof value === "boolean") return value;
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  if (["1", "true", "yes"].includes(normalized)) return true;
  if (["0", "false", "no"].includes(normalized)) return false;
  return null;
}

function firstHeader(headers: Headers, names: string[]): string | null {
  for (const name of names) {
    const value = headers.get(name);
    if (value !== null) return value;
  }
  return null;
}

function lineCount(text: string): number {
  if (text === "") return 0;
  return text.replace(/\n$/, "").split(/\r?\n/).length;
}

function readStringField(body: Record<string, unknown>, names: string[]): string | null {
  for (const name of names) {
    const value = body[name];
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(String).join("\n");
  }
  return null;
}

function readCursorField(body: Record<string, unknown>, names: string[]): string | null {
  for (const name of names) {
    const value = parseCursor(body[name]);
    if (value !== null) return value;
  }
  return null;
}

function readBooleanField(body: Record<string, unknown>, names: string[]): boolean | null {
  for (const name of names) {
    const value = parseBoolean(body[name]);
    if (value !== null) return value;
  }
  return null;
}

function logPageFromJson(body: Record<string, unknown>, requestedLines: number | null): KotxLogPage {
  const text =
    readStringField(body, ["text", "content", "log"]) ??
    (Array.isArray(body.lines) ? body.lines.map(String).join("\n") : "");
  const before =
    readCursorField(body, [
      "before",
      "nextBefore",
      "next_before",
      "startLine",
      "start_line",
      "firstLine",
      "first_line",
      "start",
      "cursor",
      "nextCursor",
      "next_cursor",
      "previousCursor",
      "previous_cursor",
    ]) ?? null;
  const explicitHasMore = readBooleanField(body, [
    "hasMoreBefore",
    "has_more_before",
    "hasMore",
    "has_more",
    "hasOlder",
    "has_older",
    "hasPrevious",
    "has_previous",
  ]);

  return {
    text,
    hasMoreBefore: explicitHasMore ?? (requestedLines !== null && lineCount(text) >= requestedLines),
    before,
  };
}

async function logRequest(path: string, params: KotxLogParams = {}): Promise<KotxLogPage> {
  const qs = new URLSearchParams();
  if (params.tail !== undefined) qs.set("tail", String(params.tail));
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

  const requestedLines = params.limit ?? params.tail ?? null;
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return logPageFromJson(parsed as Record<string, unknown>, requestedLines);
    }
  } catch {
    /* raw log text */
  }

  const before =
    parseCursor(
      firstHeader(res.headers, [
        "x-log-before",
        "x-log-next-before",
        "x-log-start-line",
        "x-log-first-line",
        "x-start-line",
        "x-log-cursor",
        "x-next-cursor",
      ]),
    ) ?? null;
  const explicitHasMore = parseBoolean(
    firstHeader(res.headers, [
      "x-log-has-more-before",
      "x-log-has-more",
      "x-has-more-before",
      "x-has-more",
      "x-log-has-older",
      "x-has-older",
    ]),
  );

  return {
    text,
    hasMoreBefore: explicitHasMore ?? (requestedLines !== null && lineCount(text) >= requestedLines),
    before,
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
  getLog: (id: number, params: KotxLogParams = {}) =>
    logRequest(`/tasks/${id}/log`, params),

  start: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/start`, { method: "POST" }),
  approve: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/approve`, { method: "POST" }),
  stop: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/stop`, { method: "POST" }),
  discard: (id: number) =>
    jsonRequest<{ ok: true }>(`/tasks/${id}/discard`, { method: "POST" }),

  listContainers: () => jsonRequest<KotxContainer[]>(`/containers`),
};
