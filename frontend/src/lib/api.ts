import type { Label, RawInput, Task } from "./types";

class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, statusText: string, body: unknown) {
    super(apiErrorMessage(status, statusText, body));
    this.status = status;
    this.body = body;
  }
}

// Always yields a non-empty message: `statusText` is empty over HTTP/2, and
// FastAPI validation errors carry a list (not a string) in `detail`.
function apiErrorMessage(
  status: number,
  statusText: string,
  body: unknown,
): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail) return JSON.stringify(detail).slice(0, 200);
  }
  if (typeof body === "string" && body.trim()) return body.trim().slice(0, 200);
  return statusText || `Request failed (HTTP ${status})`;
}

async function request<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    headers: { "content-type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) throw new ApiError(res.status, res.statusText, body);
  return body as T;
}

export const api = {
  health: () => request<{ status: string }>("/health"),
  whoami: () => request<{ email: string | null }>("/auth/whoami"),
  logout: () => request<void>("/auth/logout", { method: "POST" }),

  listTasks: (status?: string) =>
    request<Task[]>(`/tasks${status ? `?status=${status}` : ""}`),
  getTask: (id: string) => request<Task>(`/tasks/${id}`),
  createTask: (text: string) =>
    request<TaskCreationAccepted>("/tasks", {
      method: "POST",
      body: JSON.stringify({ content: text }),
    }),
  updateTask: (id: string, fields: Partial<Task>) =>
    request<Task>(`/tasks/${id}`, {
      method: "PATCH",
      body: JSON.stringify(fields),
    }),
  locationSuggestions: (query: string) =>
    request<LocationSuggestions>(
      `/tasks/location_suggestions?q=${encodeURIComponent(query)}`,
    ),
  rescheduleTask: (id: string) =>
    request<Task>(`/tasks/${id}/reschedule`, { method: "POST" }),
  createGithubIssue: (id: string) =>
    request<Task>(`/tasks/${id}/github_issue`, { method: "POST" }),
  closeTask: (id: string) =>
    request<void>(`/tasks/${id}/close`, { method: "POST" }),
  markNotTask: (id: string) =>
    request<void>(`/tasks/${id}/not_task`, { method: "POST" }),
  reopenTask: (id: string) =>
    request<TaskCreationAccepted>(`/tasks/${id}/reopen`, { method: "POST" }),

  listInputs: (
    params: { limit?: number; status?: string; source?: string } = {},
  ) => {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.status) qs.set("status", params.status);
    if (params.source) qs.set("source", params.source);
    return request<RawInput[]>(`/inputs?${qs.toString()}`);
  },
  getInput: (id: string) => request<RawInput>(`/inputs/${id}`),
  promoteInput: (
    id: string,
    opts?: {
      title?: string;
      contextInputIds?: string[];
      targetTaskId?: string;
    },
  ) =>
    request<TaskCreationAccepted>(`/tasks/open/${id}`, {
      method: "POST",
      body: JSON.stringify({
        ...(opts?.title ? { title: opts.title } : {}),
        ...(opts?.contextInputIds?.length
          ? { context_input_ids: opts.contextInputIds }
          : {}),
        ...(opts?.targetTaskId ? { target_task_id: opts.targetTaskId } : {}),
      }),
    }),
  discardKotxRun: (rawInputId: string) =>
    request<{ discarded: boolean }>(`/inputs/${rawInputId}/discard_kotx`, {
      method: "POST",
    }),
  unreadInputCount: () => request<UnreadInputs>("/inputs/unread_count"),
  markInputsSeen: () =>
    request<UnreadInputs>("/inputs/mark_seen", { method: "POST" }),

  listLabels: () => request<Label[]>("/labels"),

  getPoints: () => request<{ total: number }>("/points"),
  getPointsLog: (limit = 50) =>
    request<PointsLog>(`/points/log?limit=${limit}`),
  markPointsLogSeen: () =>
    request<PointsLogSeen>("/points/log/mark_seen", { method: "POST" }),
  adjustPoints: (
    amount: number,
    metadata: { caller?: string; reason?: string } = {},
  ) =>
    request<{ total: number }>("/points/adjust", {
      method: "POST",
      body: JSON.stringify({ amount, ...metadata }),
    }),

  getSettings: () => request<AppSettings>("/settings"),
  updateSettings: (patch: Partial<AppSettings>) =>
    request<AppSettings>("/settings", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
};

export interface AppSettings {
  auto_poll_enabled: boolean;
}

export interface TaskCreationAccepted {
  raw_input_id: string;
  status: string;
}

export interface LocationSuggestions {
  suggestions: string[];
}

export interface UnreadInputs {
  count: number;
  last_seen_at: string;
}

export interface PointsLogEntry {
  id: string;
  amount: number;
  source: string;
  reason: string;
  caller: string | null;
  created_at: string;
}

export interface PointsLog {
  entries: PointsLogEntry[];
  count: number;
  last_seen_at: string;
}

export interface PointsLogSeen {
  count: number;
  last_seen_at: string;
}
