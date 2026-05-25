export type TaskStatus = "open" | "duplicate" | "closed" | "not_task";

export type AiDoable = "yes" | "no" | "unsure";

export interface Task {
  id: string;
  title: string;
  description: string | null;
  link: string | null;
  due_date: string | null;
  estimation: number | null;
  location: string | null;
  label: string | null;
  ai_doable: AiDoable | null;
  status: TaskStatus;
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
  agent_trace: AgentTrace | null;
}

export interface SourcePollResult {
  source: string;
  fetched: number;
  tasks_created: number;
  skipped: number;
  errors: string[];
}
