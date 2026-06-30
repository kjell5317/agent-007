export type TaskStatus = "open" | "duplicate" | "closed" | "not_task";

export interface Task {
  id: string;
  title: string;
  description: string | null;
  link: string | null;
  due_date: string | null;
  estimation: number | null;
  location: string | null;
  label: string | null;
  status: TaskStatus;
  is_manual: boolean;
  created_at: string;
  updated_at: string;
}

export interface Label {
  name: string;
  description: string;
  color: string;
}

export interface AgentTrace {
  outcome?: string;
  reason?: string;
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
  status: "processing" | "not_task" | "duplicate" | "open" | "closed";
  task_id: string | null;
  task_title: string | null;
  agent_trace: AgentTrace | null;
}

