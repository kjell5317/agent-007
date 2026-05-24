import type { RawInput, SourcePollResult, Task } from "./types";

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
  createTask: (title: string) =>
    request<Task>("/tasks", {
      method: "POST",
      body: JSON.stringify({
        title,
        description: null,
        estimation: null,
        due_date: null,
        location: null,
        link: null,
      }),
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

  listInputs: (params: { limit?: number; status?: string; source?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.status) qs.set("status", params.status);
    if (params.source) qs.set("source", params.source);
    return request<RawInput[]>(`/inputs?${qs.toString()}`);
  },
  promoteInput: (id: string, title: string) =>
    request<Task>(`/inputs/${id}/open_task`, {
      method: "POST",
      body: JSON.stringify({
        title,
        description: null,
        due_date: null,
        estimation: null,
        location: null,
        link: null,
      }),
    }),

  poll: (source: string) =>
    request<SourcePollResult>(`/sources/poll?source=${source}`, { method: "POST" }),
};
