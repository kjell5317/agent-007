import type { Label, RawInput, SourcePollResult, Task } from "./types";

class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, statusText: string, body: unknown) {
    const detail =
      (body && typeof body === "object" && "detail" in body && (body as { detail: unknown }).detail) ||
      statusText;
    super(typeof detail === "string" ? detail : statusText);
    this.status = status;
    this.body = body;
  }
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
  createTask: (text: string) =>
    request<TaskCreationAccepted>("/tasks", {
      method: "POST",
      body: JSON.stringify({ title: text }),
    }),
  updateTask: (id: string, fields: Partial<Task>) =>
    request<Task>(`/tasks/${id}`, {
      method: "PATCH",
      body: JSON.stringify(fields),
    }),
  closeTask: (id: string) =>
    request<void>(`/tasks/${id}/close`, { method: "POST" }),
  markNotTask: (id: string) =>
    request<void>(`/tasks/${id}/not_task`, { method: "POST" }),
  reopenTask: (id: string) =>
    request<void>(`/tasks/${id}/reopen`, { method: "POST" }),

  listInputs: (params: { limit?: number; status?: string; source?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.status) qs.set("status", params.status);
    if (params.source) qs.set("source", params.source);
    return request<RawInput[]>(`/inputs?${qs.toString()}`);
  },
  getInput: (id: string) => request<RawInput>(`/inputs/${id}`),
  promoteInput: (id: string, title?: string) =>
    request<TaskCreationAccepted>(`/tasks/open/${id}`, {
      method: "POST",
      body: JSON.stringify(title ? { title } : {}),
    }),
  unreadInputCount: () =>
    request<UnreadInputs>("/inputs/unread_count"),
  markInputsSeen: () =>
    request<UnreadInputs>("/inputs/mark_seen", { method: "POST" }),

  unreadTaskCount: () =>
    request<UnreadInputs>("/tasks/unread_count"),
  markTasksSeen: () =>
    request<UnreadInputs>("/tasks/mark_seen", { method: "POST" }),

  poll: () =>
    request<SourcePollResult>("/sources/poll", { method: "POST" }),

  listLabels: () => request<Label[]>("/labels"),

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

export interface UnreadInputs {
  count: number;
  last_seen_at: string;
}

