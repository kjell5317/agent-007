import type {
  ChatCitation,
  ChatMessage,
  ChatSearchScope,
  ChatSummary,
  ChatToolTrace,
  GroceryCategory,
  GroceryPreferences,
  GrocerySettings,
  GroceryState,
  GroceryUnit,
  RecipesState,
  Label,
  LinkPreview,
  Note,
  NoteAudit,
  PauseReason,
  RawInput,
  RecurrenceUnit,
  SearchHit,
  SearchHitType,
  Task,
  HandoffKind,
  WorkSession,
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
  whoami: () => request<{ email: string | null }>("/auth/whoami"),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  slackApps: () => request<string[]>("/oauth/slack/apps"),

  listTasks: (status?: string) =>
    request<Task[]>(`/tasks${status ? `?status=${status}` : ""}`),
  getTask: (id: string) => request<Task>(`/tasks/${id}`),
  splitTask: (id: string) => request<Task>(`/tasks/${id}/split`, { method: "POST" }),
  splitTaskDeterministic: (id: string) => request<Task>(`/tasks/${id}/split_deterministic`, { method: "POST" }),
  // Deletes the subtasks; the parent becomes a normal task with the open work.
  collapseTask: (id: string) => request<Task>(`/tasks/${id}/collapse`, { method: "POST" }),
  addSubtask: (id: string, title: string) => request<Task>(`/tasks/${id}/subtasks`, {
    method: "POST", body: JSON.stringify({ title }),
  }),
  reorderSubtasks: (id: string, order: string[]) => request<Task>(`/tasks/${id}/subtasks/order`, {
    method: "PUT", body: JSON.stringify(order),
  }),
  // Set fields win over whatever the agent extracts from the text.
  createTask: (text: string, fields: TaskDraftFields = {}) =>
    request<TaskCreationAccepted>("/tasks", {
      method: "POST",
      body: JSON.stringify({ content: text, ...fields }),
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
  setRecurrence: (id: string, unit: RecurrenceUnit, interval: number) =>
    request<Task>(`/tasks/${id}/recurrence`, {
      method: "PUT",
      body: JSON.stringify({ unit, interval }),
    }),
  removeRecurrence: (id: string) =>
    request<Task>(`/tasks/${id}/recurrence`, { method: "DELETE" }),
  closeTask: (id: string) =>
    request<void>(`/tasks/${id}/close`, { method: "POST" }),
  markNotTask: (id: string) =>
    request<void>(`/tasks/${id}/not_task`, { method: "POST" }),
  // Empty body accepts the proposed hand-off; `kind` starts one by hand.
  startHandoff: (id: string, body: { kind?: HandoffKind; brief?: string } = {}) =>
    request<Task>(`/tasks/${id}/handoff`, { method: "POST", body: JSON.stringify(body) }),
  declineHandoff: (id: string) =>
    request<void>(`/tasks/${id}/handoff/decline`, { method: "POST" }),
  regenerateDraft: (id: string) =>
    request<void>(`/tasks/${id}/draft/regenerate`, { method: "POST" }),
  discardDraft: (id: string) =>
    request<void>(`/tasks/${id}/draft/discard`, { method: "POST" }),
  reopenTask: (id: string) =>
    request<TaskCreationAccepted>(`/tasks/${id}/reopen`, { method: "POST" }),

  // Starting pauses whichever other task is running.
  startTask: (id: string) => request<Task>(`/tasks/${id}/start`, { method: "POST" }),
  pauseTask: (id: string, reason: PauseReason) =>
    request<Task>(`/tasks/${id}/pause`, { method: "POST", body: JSON.stringify({ reason }) }),
  // Waiting on someone, running or not: off the calendar until resumed.
  waitTask: (id: string) => request<Task>(`/tasks/${id}/wait`, { method: "POST" }),
  resumeTask: (id: string) => request<Task>(`/tasks/${id}/resume`, { method: "POST" }),
  completeTask: (id: string) => request<Task>(`/tasks/${id}/complete`, { method: "POST" }),
  logWork: (id: string, span: WorkSpan & { complete: boolean }) =>
    request<Task>(`/tasks/${id}/sessions`, { method: "POST", body: JSON.stringify(span) }),
  listWorkSessions: (id: string) => request<WorkSession[]>(`/tasks/${id}/sessions`),
  // Spontaneous work: creates the task, logs the span and closes it.
  logSpontaneousWork: (body: WorkSpan & { title: string; label?: string }) =>
    request<Task>("/tasks/logged", { method: "POST", body: JSON.stringify(body) }),

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

  suggest: (q: string, limit = 8, types?: readonly SearchHitType[], filters?: {
    excludeSource?: string; source?: string; label?: string; status?: string; recurring?: boolean;
  }) => {
    const params = new URLSearchParams({ q, limit: String(limit) });
    if (types && types.length) params.set("types", types.join(","));
    if (filters) for (const [key, value] of Object.entries(filters)) {
      if (value) params.set(key === "excludeSource" ? "exclude_source" : key, String(value));
    }
    return request<{ hits: SearchHit[] }>(`/search/suggest?${params}`);
  },
  // The Enter search: every source the filter kind allows, sorted by relevance.
  search: (q: string, filters: {
    kind?: string | null; source?: string; label?: string; format?: string; status?: string; recurring?: boolean;
  } = {}) => {
    const params = new URLSearchParams({ q });
    for (const [key, value] of Object.entries(filters)) if (value) params.set(key, String(value));
    return request<{ hits: SearchHit[] }>(`/search?${params}`);
  },
  suggestExternal: (q: string, limit: number, kind: "github" | "calendar") =>
    request<{ hits: SearchHit[] }>(
      `/search/suggest/external?${new URLSearchParams({ q, limit: String(limit), kind })}`,
    ),
  searchFacets: (kind: "messages" | "files" | "notes") =>
    request<{ options: string[] }>(`/search/facets?kind=${kind}`),

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
  syncLabels: () => request<Label[]>("/labels/sync", { method: "POST" }),
  // Replaces all four fields, so callers send the untouched ones unchanged.
  updateLabel: (
    googleId: string,
    body: Pick<Label, "description" | "github_repo" | "default_work_type" | "default_effort">,
  ) =>
    request<Label>(`/labels/${encodeURIComponent(googleId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  grocery: () => request<GroceryState>("/grocery"),
  addGroceryItem: (text: string) =>
    request<GroceryState>("/grocery/items", { method: "POST", body: JSON.stringify({ text }) }),
  updateGroceryItem: (id: string, patch: GroceryItemPatch & { category?: GroceryCategory }) =>
    request<GroceryState>(`/grocery/items/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteGroceryItem: (id: string) =>
    request<GroceryState>(`/grocery/items/${id}`, { method: "DELETE" }),
  crossOffGroceryItem: (id: string) =>
    request<GroceryState>(`/grocery/items/${id}/cross-off`, { method: "POST" }),
  uncrossGroceryItem: (id: string) =>
    request<GroceryState>(`/grocery/items/${id}/uncross`, { method: "POST" }),
  updateStorageItem: (id: string, patch: GroceryItemPatch & { expires_on?: string }) =>
    request<GroceryState>(`/grocery/storage/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  useStoragePortion: (id: string) =>
    request<GroceryState>(`/grocery/storage/${id}/use`, { method: "POST" }),
  removeStorageItem: (id: string) =>
    request<GroceryState>(`/grocery/storage/${id}`, { method: "DELETE" }),
  setGroceryStaple: (productKey: string, staple: boolean) =>
    request<GroceryState>(`/grocery/products/${encodeURIComponent(productKey)}/staple`, {
      method: "PUT", body: JSON.stringify({ staple }),
    }),
  grocerySettings: () => request<GrocerySettings>("/grocery/settings"),
  // An empty store_location clears it.
  updateGrocerySettings: (patch: Partial<GrocerySettings>) =>
    request<GrocerySettings>("/grocery/settings", { method: "PATCH", body: JSON.stringify(patch) }),
  groceryRecipes: () => request<RecipesState>("/grocery/recipes"),
  replanGroceries: () => request<RecipesState>("/grocery/plan", { method: "POST" }),
  setRecipeDone: (id: string, done: boolean) =>
    request<RecipesState>(`/grocery/recipes/${id}/done`, {
      method: "PUT", body: JSON.stringify({ done }),
    }),
  groceryPreferences: () => request<GroceryPreferences>("/grocery/preferences"),
  rateRecipe: (id: string, rating: -1 | 0 | 1) =>
    request<RecipesState>(`/grocery/recipes/${id}/rating`, {
      method: "PUT", body: JSON.stringify({ rating }),
    }),

  listNotes: (limit = 500, source = "", query = "") =>
    request<Note[]>(`/notes?${new URLSearchParams({ limit: String(limit), ...(source ? { source } : {}), ...(query ? { query } : {}) })}`),
  getNote: (id: string) => request<Note>(`/notes/${id}`),
  noteHistory: (id: string) => request<NoteAudit[]>(`/notes/${id}/history`),
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
  updateSettings: (patch: Partial<Pick<AppSettings, "auto_poll_enabled">>) =>
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
  scope?: ChatSearchScope,
): Promise<void> {
  const res = await fetch("/search/chat", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ messages, ...(scope ? { scope } : {}) }),
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
  user_timezone: string;
}

export interface WorkSpan {
  started_at: string;
  ended_at: string;
}

export type TaskDraftFields = Partial<Pick<Task, "due_date" | "label" | "estimation">>;

export interface TaskCreationAccepted {
  raw_input_id: string;
  status: string;
}

// A unit change needs the portion in the new unit too.
export interface GroceryItemPatch {
  name?: string;
  quantity?: number;
  unit?: GroceryUnit;
  portion?: number;
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
