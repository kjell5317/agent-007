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
