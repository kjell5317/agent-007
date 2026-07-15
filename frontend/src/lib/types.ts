export type TaskStatus = "open" | "duplicate" | "closed" | "not_task";
export type TaskScheduleStatus = "scheduled" | "unscheduled";

export interface Task {
  id: string;
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
  status: TaskStatus;
  is_manual: boolean;
  kotx_task_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface TaskRawInput extends RawInput {
  source_url: string | null;
}

export interface Label {
  name: string;
  description: string;
  color: string;
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

export type SearchHitType = "task" | "note" | "input" | "document" | "drive";

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
  created_at: string;
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
