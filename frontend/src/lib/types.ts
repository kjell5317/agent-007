export type TaskStatus = "open" | "duplicate" | "closed" | "not_task";
// "deferred": due after the planning horizon, deliberately not placed yet and
// reconsidered on every solve — not a warning like "unscheduled".
export type TaskScheduleStatus = "scheduled" | "pending" | "unscheduled" | "deferred";
export type RecurrenceUnit = "day" | "week" | "month";
export type WorkType = "physical" | "cognitive" | "social";
export type Effort = "low" | "medium" | "high";
export type DeadlineKind = "hard" | "soft";
// "waiting": paused while waiting on someone, off the calendar until resumed.
export type ScheduleState = "placed" | "deferred" | "unplaced" | "waiting";
export type PauseReason = "break" | "waiting" | "interrupted";

export interface WorkSession {
  id: string;
  task_id: string;
  started_at: string;
  ended_at: string | null;
  // A pause reason, or "switched" / "completed" / "logged" / "abandoned".
  end_reason: string | null;
  // "retro": logged afterwards rather than tracked live.
  source: "live" | "retro";
}

export interface SubtaskSummary {
  id: string;
  public_id: string | null;
  title: string;
  due_date: string;
  estimation: number | null;
  status: TaskStatus;
  depends_on_task_id: string | null;
}

export type HandoffKind = "research" | "email_reply" | "code_change";

// Work an agent does on a task after it's created.
export interface TaskHandoff {
  id: string;
  kind: HandoffKind;
  brief: string;
  state: "proposed" | "running" | "ready" | "failed" | "declined";
  result: Record<string, unknown> | null;
  // The agent's tool calls, shaped like a raw input's agent_trace.
  trace: AgentTrace | null;
  created_at: string;
  updated_at: string;
}

export interface Task {
  id: string;
  public_id: string | null;
  parent_card: Task | null;
  title: string;
  description: string | null;
  link: string | null;
  source_url: string | null;
  raw_inputs: TaskRawInput[];
  due_date: string | null;
  scheduled_date: string | null;
  schedule_status: TaskScheduleStatus;
  estimation: number | null;
  location: string | null;
  label: string | null;
  related_event_id: string | null;
  related_event_calendar_id: string | null;
  related_event_due_derived: boolean;
  parent_task_id: string | null;
  depends_on_task_id: string | null;
  is_container: boolean;
  due_date_derived: boolean;
  subtask_order: number | null;
  recurrence_unit: RecurrenceUnit | null;
  recurrence_interval: number | null;
  // Shared by every instance of a repeating task.
  recurrence_series_id: string | null;
  subtasks: SubtaskSummary[];
  status: TaskStatus;
  is_manual: boolean;
  kotx_task_id: number | null;
  work_type: WorkType | null;
  effort: Effort | null;
  deadline_kind: DeadlineKind;
  placed_by: "planner" | "user";
  schedule_state: ScheduleState;
  // Set while a work session runs.
  running_since: string | null;
  waiting_since: string | null;
  // Finished sessions only; a running one adds `now - running_since`.
  worked_minutes: number;
  // What the planner books after the learned estimate correction; null while
  // no correction is fitted.
  estimation_corrected: number | null;
  // Newest first.
  handoffs: TaskHandoff[];
  created_at: string;
  updated_at: string;
}

export interface TaskRawInput extends RawInput {
  source_url: string | null;
}

export interface Label {
  // Opaque Google Calendar event-label id; name and color are Google's and
  // read-only here, the rest is ours.
  google_id: string;
  name: string;
  color: string;
  description: string;
  github_repo: string | null;
  default_work_type: WorkType | null;
  default_effort: Effort | null;
}

export interface AgentTrace {
  outcome?: string;
  reason?: string;
  // Set by the embedding auto-decider (no LLM) when it links an input as a
  // duplicate on similarity alone — distinguishes it from the agent's own
  // no_change / updated / reopened / closed decisions.
  auto_decided?: boolean;
  [k: string]: unknown;
}

export type SearchHitType = "task" | "note" | "input" | "document" | "drive" | "contact" | "github";

export interface ChatSearchScope {
  kind: "tasks" | "messages" | "notes" | "events" | "files" | "contacts" | "github";
  label?: string;
  source?: string;
  format?: string;
}

export interface SearchHit {
  type: SearchHitType;
  id: string;
  title: string;
  snippet: string | null;
  url: string | null;
  // Present for task hits (its own id) and for input hits linked to a task —
  // clicking either opens that task.
  task_id: string | null;
  // Unified origin: input source (gmail/slack/…) or document provider
  // (calendar/…); for tasks, the source of their most recent input.
  source: string | null;
  // Sender of the (last, for tasks) input; null for documents/manual.
  sender: string | null;
  // Lifecycle status: task/input status, or "event" for calendar documents.
  status: string | null;
  ts: string | null;
  score: number;
  meta?: ChatCitationMeta | null;
}

// Chat / "ask" mode. A citation is a retrieved hit the answer can reference by
// its `tag` (e.g. "T1"); `type` widens SearchHitType with "drive".
export interface ChatCitation {
  tag: string;
  type: string;
  id: string;
  title: string;
  snippet: string | null;
  url: string | null;
  task_id: string | null;
  source: string | null;
  sender: string | null;
  status: string | null;
  ts: string | null;
  // Source-specific extras the citation widgets render (contact emails/phones/
  // birthday/address/org, event start/location, drive mime). Absent for hits
  // that set no extras.
  meta?: ChatCitationMeta | null;
}

export interface ChatCitationMeta {
  emails?: string[];
  phones?: string[];
  addresses?: string[];
  org?: string;
  birthday?: string;
  relations?: string[];
  start?: string;
  end?: string;
  location?: string;
  mime?: string;
  due_date?: string;
  label_id?: string;
  similarity?: number;
  [k: string]: unknown;
}

export interface ChatToolTrace {
  name: string;
  status: "success" | "failed";
  purpose: string;
  result_summary: string;
  // Raw call input and full result text, expandable from the tool chip.
  params?: Record<string, unknown>;
  result?: string;
  changed_state: boolean;
  artifact_refs: string[];
}

// Server-fetched unfurl of a link in an assistant answer (WhatsApp-style card).
export interface LinkPreview {
  url: string;
  title: string;
  description: string | null;
  site_name: string | null;
  image: string | null;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  citations: ChatCitation[];
  tools: ChatToolTrace[];
  // Assistant message still streaming (spinner until the first token lands).
  pending: boolean;
}

// A persisted conversation as shown in the recent-chats list.
export interface ChatSummary {
  id: string;
  title: string;
  updated_at: string;
}

// A standalone fact an agent flow extracted — the agent's long-term memory.
// Surfaced on the Notes tab so it can be audited, edited, or deleted by hand.
export interface Note {
  id: string;
  content: string;
  source: string | null;
  source_from: string | null;
  source_subject: string | null;
  source_raw_input_id: string | null;
  source_raw_input_ids: string[];
  needs_review: boolean;
  history_count: number;
  content_update_count: number;
  last_content_update_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface NoteAudit {
  action: string;
  actor: string;
  occurred_at: string;
  old_content: string | null;
  new_content: string | null;
  source_raw_input_id: string | null;
  needs_review: boolean | null;
}

export interface RawInput {
  id: string;
  source: string;
  external_id: string | null;
  content: string;
  source_metadata: Record<string, unknown> & {
    subject?: string;
    from?: string;
    thread_id?: string;
  };
  received_at: string;
  processed_at: string | null;
  status: "processing" | "not_task" | "duplicate" | "open" | "closed" | "event";
  task_id: string | null;
  task_title: string | null;
  agent_trace: AgentTrace | null;
}

// In the order of a walk through a typical store.
export type GroceryCategory =
  | "produce" | "bakery" | "dairy" | "meat_fish" | "dry_goods" | "canned"
  | "spices_baking" | "frozen" | "drinks" | "snacks" | "household" | "other";

// Amount and portion share one unit.
export type GroceryUnit = "g" | "ml" | "piece" | "pack" | "can" | "jar" | "bottle" | "bunch";

export interface GroceryItem {
  id: string;
  product_key: string;
  name: string;
  quantity: number;
  unit: GroceryUnit;
  portion: number;
  category: GroceryCategory;
  origin: "manual" | "agent" | "staple";
  staple: boolean;
  created_at: string;
}

export interface StorageItem {
  id: string;
  product_key: string;
  name: string;
  quantity: number;
  unit: GroceryUnit;
  portion: number;
  category: GroceryCategory;
  // YYYY-MM-DD, the last day it's good.
  expires_on: string;
  added_at: string;
  staple: boolean;
}

export interface GroceryState {
  items: GroceryItem[];
  storage: StorageItem[];
}
