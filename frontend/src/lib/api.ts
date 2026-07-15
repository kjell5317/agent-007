import type {
  ChatCitation,
  ChatMessage,
  ChatSummary,
  ChatToolTrace,
  Label,
  LinkPreview,
  Note,
  RawInput,
  SearchHit,
  SearchHitType,
  Task,
} from "./types";

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

  suggest: (q: string, limit = 8, types?: readonly SearchHitType[]) => {
    const params = new URLSearchParams({ q, limit: String(limit) });
    if (types && types.length) params.set("types", types.join(","));
    return request<{ hits: SearchHit[] }>(`/search/suggest?${params}`);
  },

  chatStream,

  getLinkPreview: (url: string) =>
    request<{ preview: LinkPreview | null }>(
      `/search/link_preview?url=${encodeURIComponent(url)}`,
    ),

  listChats: (limit = 5) => request<ChatSummary[]>(`/search/chats?limit=${limit}`),
  getChat: (id: string) =>
    request<ChatSummary & { messages: ChatMessage[] }>(`/search/chats/${id}`),
  createChat: (body: { title: string; messages: ChatMessage[] }) =>
    request<ChatSummary>("/search/chats", { method: "POST", body: JSON.stringify(body) }),
  updateChat: (id: string, body: { title: string; messages: ChatMessage[] }) =>
    request<ChatSummary>(`/search/chats/${id}`, { method: "PUT", body: JSON.stringify(body) }),

  listLabels: () => request<Label[]>("/labels"),

  listNotes: (limit = 500) => request<Note[]>(`/notes?limit=${limit}`),
  updateNote: (id: string, content: string) =>
    request<Note>(`/notes/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ content }),
    }),
  deleteNote: (id: string) =>
    request<void>(`/notes/${id}`, { method: "DELETE" }),

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

export interface ChatStreamHandlers {
  onCitations: (items: ChatCitation[]) => void;
  onToken: (text: string) => void;
  onTool: (trace: ChatToolTrace) => void;
  onError: (message: string) => void;
}

// POST the conversation and consume the SSE response (`citations` / `token` /
// `tool_call` / `error` / `done`). EventSource is GET-only, so we read the body
// stream ourselves. Resolves when the stream ends (or aborts).
async function chatStream(
  messages: {
    role: string;
    content: string;
    tools?: {
      name: string;
      params: Record<string, unknown>;
      result: string;
      status: string;
    }[];
  }[],
  signal: AbortSignal,
  handlers: ChatStreamHandlers,
): Promise<void> {
  const res = await fetch("/search/chat", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ messages }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, res.statusText, await res.text().catch(() => null));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line; process each complete one.
    for (;;) {
      const sep = /\r?\n\r?\n/.exec(buffer);
      if (!sep) break;
      const frame = buffer.slice(0, sep.index);
      buffer = buffer.slice(sep.index + sep[0].length);
      dispatchFrame(frame, handlers);
    }
  }
}

function dispatchFrame(frame: string, h: ChatStreamHandlers): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith(":")) continue; // comment / keep-alive ping
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (dataLines.length === 0) return;
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    return;
  }
  switch (event) {
    case "citations":
      h.onCitations((data.items as ChatCitation[]) ?? []);
      break;
    case "token":
      h.onToken((data.text as string) ?? "");
      break;
    case "tool_call":
      h.onTool(data as unknown as ChatToolTrace);
      break;
    case "error":
      h.onError((data.message as string) ?? "Something went wrong");
      break;
    // "done" needs no payload handling — the stream simply ends after it.
  }
}

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
  task_id: string | null;
  created_at: string;
}

export interface PointsLog {
  entries: PointsLogEntry[];
  count: number;
  last_seen_at: string;
  has_more: boolean;
}

export interface PointsLogSeen {
  count: number;
  last_seen_at: string;
}
