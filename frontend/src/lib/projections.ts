import { toYaml } from "@/lib/format";

export interface ProjectionField {
  label: string;
  value: string;
}

export interface EvidenceRow {
  id: string;
  kind: "candidate" | "precedent";
  title: string;
  status?: string;
  source?: string;
  similarity?: string;
  taskId?: string;
  sender?: string;
  receivedAt?: string;
  snippet?: string;
  selected?: boolean;
}

export interface ToolRow {
  id: string;
  name: string;
  status: "success" | "failed" | "skipped" | "denied" | "timed_out" | "called";
  purpose: string;
  input?: string;
  result?: string;
  changedState?: boolean;
  artifacts: string[];
}

export interface TraceProjection {
  summary: ProjectionField[];
  evidence: EvidenceRow[];
  tools: ToolRow[];
  diagnostics: string;
}

export interface MetadataProjection {
  fields: ProjectionField[];
  diagnostics: string;
}

export interface LogRow {
  id: string;
  kind: "decision" | "tool" | "action" | "event";
  status: string;
  title: string;
  subtitle?: string;
  body?: string;
  raw: string;
}

export interface KotxLogProjection {
  rows: LogRow[];
  diagnostics: string;
  rawFallback: string;
  infrastructureCount: number;
}

type JsonRecord = Record<string, unknown>;

const METADATA_KEYS: [string, string][] = [
  ["from", "Sender"],
  ["sender", "Sender"],
  ["to", "To"],
  ["recipients", "Recipients"],
  ["cc", "Cc"],
  ["subject", "Subject"],
  ["date", "Date"],
  ["received_at", "Received"],
  ["thread_id", "Thread"],
  ["channel", "Channel"],
  ["channel_id", "Channel"],
  ["account", "Account"],
  ["url", "URL"],
  ["link", "URL"],
  ["permalink", "URL"],
  ["attachments", "Attachments"],
  ["attachment_count", "Attachments"],
  ["is_dm", "Direct message"],
  ["direct_message", "Direct message"],
];

const INFRA_KEYS = new Set([
  "cache",
  "cache_control",
  "embedding",
  "embedded_query",
  "llm",
  "meta",
  "model",
  "provider",
  "retry",
  "retries",
  "timing",
  "tokens",
  "usage",
]);

export function projectSourceMetadata(
  source: string,
  externalId: string | null,
  metadata: JsonRecord | null | undefined,
): MetadataProjection {
  const fields: ProjectionField[] = [{ label: "Source", value: source }];
  if (externalId) fields.push({ label: "External id", value: externalId });

  const seen = new Set<string>();
  const meta = metadata ?? {};
  for (const [key, label] of METADATA_KEYS) {
    if (seen.has(label)) continue;
    const rendered = renderValue(meta[key]);
    if (!rendered) continue;
    seen.add(label);
    fields.push({ label, value: rendered });
  }

  return { fields, diagnostics: toYaml(meta) };
}

export function projectAgentTrace(trace: unknown): TraceProjection {
  const record = asRecord(trace) ?? {};
  const evidence = collectEvidence(record);
  const tools = collectTools(record);
  const selected = evidence.find((row) => row.selected);
  const summary: ProjectionField[] = [];

  addField(summary, "Branch", stringValue(record.branch));
  addField(summary, "Goal", stringValue(record.goal) ?? stringValue(record.current_step));
  addField(
    summary,
    "Decision",
    stringValue(record.outcome) ?? stringValue(record.action) ?? stringValue(record.status),
  );
  addField(
    summary,
    "Task",
    stringValue(record.task_id) ?? stringValue(record.existing_task_id),
  );
  if (selected) addField(summary, "Selected evidence", `${selected.id} ${selected.title}`);
  addField(summary, "Reason", stringValue(record.reason));
  addField(summary, "Confidence", confidenceValue(record.confidence));

  if (summary.length === 0) {
    summary.push({ label: "Trace", value: "No decision summary was recorded." });
  }

  return {
    summary,
    evidence,
    tools,
    diagnostics: toYaml(trace),
  };
}

export function projectKotxLog(text: string | null): KotxLogProjection {
  if (!text?.trim()) {
    return { rows: [], diagnostics: "", rawFallback: "", infrastructureCount: 0 };
  }

  const records = parseJsonRecords(text);
  if (records.length === 0) {
    return {
      rows: [],
      diagnostics: "",
      rawFallback: text,
      infrastructureCount: 0,
    };
  }

  const rows: LogRow[] = [];
  const diagnostics: string[] = [];
  let infrastructureCount = 0;

  records.forEach((record, index) => {
    const row = logRow(record, index);
    if (row && !isInfrastructureLog(record)) {
      rows.push(row);
    } else {
      infrastructureCount += 1;
      diagnostics.push(toYaml(record));
    }
  });

  return {
    rows,
    diagnostics: diagnostics.join("\n---\n"),
    rawFallback: "",
    infrastructureCount,
  };
}

function collectEvidence(trace: JsonRecord): EvidenceRow[] {
  const rows: EvidenceRow[] = [];
  const selectedId =
    stringValue(trace.selected_evidence_ref) ??
    stringValue(trace.precedent_id) ??
    stringValue(trace.selected_precedent_id);

  const refs = arrayValue(trace.evidence_refs);
  refs.forEach((entry, index) => {
    const ref = asRecord(entry);
    if (!ref) return;
    const row = evidenceFromRecord(ref, `evidence-${index + 1}`, "candidate");
    row.selected = Boolean(row.id && selectedId && row.id.includes(selectedId));
    rows.push(row);
  });

  const selected = asRecord(trace.selected_precedent);
  if (selected) {
    const row = evidenceFromRecord(selected, "precedent", "precedent");
    row.selected = true;
    rows.push(row);
  } else if (trace.precedent_id) {
    rows.push(
      evidenceFromRecord(
        {
          id: trace.precedent_id,
          title: trace.precedent_title,
          snippet: trace.precedent_snippet,
          source: trace.precedent_source,
          sender: trace.precedent_sender,
          received_at: trace.precedent_received_at,
          similarity: trace.precedent_similarity,
          status: trace.precedent_status,
          task_id: trace.existing_task_id,
        },
        "precedent",
        "precedent",
      ),
    );
  }

  arrayValue(trace.candidates).forEach((entry, index) => {
    const candidate = asRecord(entry);
    if (!candidate) return;
    const row = evidenceFromRecord(candidate, `candidate-${index + 1}`, "candidate");
    row.selected = row.selected || Boolean(row.id && selectedId && row.id.includes(selectedId));
    rows.push(row);
  });

  return dedupeEvidence(rows);
}

function evidenceFromRecord(
  record: JsonRecord,
  fallbackId: string,
  kind: EvidenceRow["kind"],
): EvidenceRow {
  const rawId = stringValue(record.row_id) ?? stringValue(record.ref) ?? stringValue(record.id);
  const id = rawId ?? fallbackId;
  const title =
    stringValue(record.title) ??
    stringValue(record.subject) ??
    truncate(stringValue(record.snippet) ?? "", 96) ??
    "(untitled evidence)";

  return {
    id,
    kind: stringValue(record.kind) === "precedent" ? "precedent" : kind,
    title,
    status: stringValue(record.status),
    source: stringValue(record.source),
    similarity: similarityValue(record.similarity ?? record.sim),
    taskId: stringValue(record.task_id),
    sender: stringValue(record.sender) ?? stringValue(record.from),
    receivedAt: stringValue(record.received_at),
    snippet: truncate(stringValue(record.snippet) ?? stringValue(record.content_snippet) ?? "", 240),
    selected: Boolean(record.selected),
  };
}

function collectTools(trace: JsonRecord): ToolRow[] {
  const rows: ToolRow[] = [];
  arrayValue(trace.iterations).forEach((entry, iterIndex) => {
    const iteration = asRecord(entry);
    if (!iteration) return;
    const results = arrayValue(iteration.tool_results)
      .map(asRecord)
      .filter((result): result is JsonRecord => Boolean(result));
    rows.push(...toolRowsFromBlocks(iteration.blocks, results, `i${iterIndex + 1}`));
  });
  rows.push(...toolRowsFromBlocks(trace.blocks, arrayValue(trace.tool_results).map(asRecord), "top"));
  return rows;
}

function toolRowsFromBlocks(
  blocks: unknown,
  results: Array<JsonRecord | null>,
  prefix: string,
): ToolRow[] {
  const resultQueues = new Map<string, JsonRecord[]>();
  results.forEach((result) => {
    if (!result) return;
    const name = stringValue(result.name) ?? stringValue(result.tool);
    if (!name) return;
    resultQueues.set(name, [...(resultQueues.get(name) ?? []), result]);
  });

  return arrayValue(blocks).flatMap((entry, index) => {
    const block = asRecord(entry);
    if (!block || stringValue(block.type) !== "tool_use") return [];
    const name = stringValue(block.name) ?? "tool";
    const result = resultQueues.get(name)?.shift();
    const input = asRecord(block.input);
    return [
      {
        id: stringValue(block.id) ?? `${prefix}-tool-${index + 1}`,
        name,
        status: normalizeToolStatus(result),
        purpose: stringValue(result?.purpose) ?? toolPurpose(name, input),
        input: redactPreview(input ?? block.input),
        result: stringValue(result?.result_summary) ?? stringValue(result?.preview),
        changedState: booleanValue(result?.changed_state),
        artifacts: artifactRefs(result),
      },
    ];
  });
}

function logRow(record: JsonRecord, index: number): LogRow | null {
  const message =
    stringValue(record.message) ??
    stringValue(record.msg) ??
    stringValue(record.event) ??
    stringValue(record.type);
  const event = `${stringValue(record.event) ?? ""} ${stringValue(record.type) ?? ""}`.toLowerCase();
  const toolName =
    stringValue(record.tool) ??
    stringValue(record.tool_name) ??
    stringValue(asRecord(record.tool_call)?.name);
  const status =
    stringValue(record.status) ??
    stringValue(record.level) ??
    stringValue(record.outcome) ??
    "event";

  if (toolName || event.includes("tool")) {
    return {
      id: `log-${index + 1}`,
      kind: "tool",
      status,
      title: toolName ?? message ?? "Tool call",
      subtitle: message && toolName ? message : undefined,
      body: truncate(stringValue(record.result) ?? stringValue(record.preview) ?? "", 280),
      raw: toYaml(record),
    };
  }

  if (event.includes("decision") || event.includes("outcome") || record.outcome) {
    return {
      id: `log-${index + 1}`,
      kind: "decision",
      status,
      title: message ?? stringValue(record.outcome) ?? "Decision",
      body: truncate(stringValue(record.reason) ?? "", 280),
      raw: toYaml(record),
    };
  }

  if (event.includes("action") || event.includes("state") || record.action) {
    return {
      id: `log-${index + 1}`,
      kind: "action",
      status,
      title: message ?? stringValue(record.action) ?? "Action",
      body: truncate(stringValue(record.detail) ?? stringValue(record.reason) ?? "", 280),
      raw: toYaml(record),
    };
  }

  if (message) {
    return {
      id: `log-${index + 1}`,
      kind: "event",
      status,
      title: message,
      raw: toYaml(record),
    };
  }

  return null;
}

function parseJsonRecords(text: string): JsonRecord[] {
  const trimmed = text.trim();
  try {
    const parsed = JSON.parse(trimmed);
    if (Array.isArray(parsed)) return parsed.map(asRecord).filter(isPresent);
    const record = asRecord(parsed);
    if (!record) return [];
    const entries = arrayValue(record.entries).map(asRecord).filter(isPresent);
    return entries.length > 0 ? entries : [record];
  } catch {
    // Try JSONL below.
  }

  const records: JsonRecord[] = [];
  for (const line of trimmed.split(/\r?\n/)) {
    const part = line.trim();
    if (!part) continue;
    try {
      const record = asRecord(JSON.parse(part));
      if (record) records.push(record);
    } catch {
      return [];
    }
  }
  return records;
}

function isInfrastructureLog(record: JsonRecord): boolean {
  const text = [
    stringValue(record.message),
    stringValue(record.event),
    stringValue(record.type),
    stringValue(record.component),
  ]
    .filter(isPresent)
    .join(" ")
    .toLowerCase();
  if (/(cache|embed|embedding|token|usage|cost|retry|timing|latency|heartbeat|poll)/.test(text)) {
    return true;
  }
  return Object.keys(record).some((key) => INFRA_KEYS.has(key.toLowerCase()));
}

function normalizeToolStatus(result: JsonRecord | undefined): ToolRow["status"] {
  const raw = `${stringValue(result?.status) ?? ""} ${stringValue(result?.error) ?? ""}`.toLowerCase();
  if (raw.includes("timeout") || raw.includes("timed_out")) return "timed_out";
  if (raw.includes("denied") || raw.includes("reject")) return "denied";
  if (raw.includes("skip")) return "skipped";
  if (raw.includes("fail") || raw.includes("error") || raw.includes("true")) return "failed";
  if (raw.includes("success") || raw.includes("ok") || raw.includes("false")) return "success";
  return result ? "success" : "called";
}

function toolPurpose(name: string, input: JsonRecord | null | undefined): string {
  if (name === "search_notes") return `Search notes for "${truncate(stringValue(input?.query) ?? "", 80)}"`;
  if (name === "find_calendar_events") return "Find calendar conflicts";
  if (name === "create_event") return `Create event "${stringValue(input?.summary) ?? "event"}"`;
  if (name === "create_task") return `Create task "${stringValue(input?.title) ?? "task"}"`;
  if (name === "update_task") return "Update existing task";
  if (name === "mark_not_task") return "Mark as not a task";
  if (name === "no_change") return "Leave existing task unchanged";
  return titleize(name);
}

function artifactRefs(result: JsonRecord | undefined): string[] {
  if (!result) return [];
  const refs = arrayValue(result.artifact_refs).map(renderValue).filter(isPresent);
  const eventId = stringValue(result.event_id);
  if (eventId) refs.push(`event:${eventId}`);
  return refs;
}

function redactPreview(value: unknown): string | undefined {
  const record = asRecord(value);
  if (!record) return truncate(renderValue(value) ?? "", 220);
  const redacted: JsonRecord = {};
  for (const [key, entry] of Object.entries(record)) {
    if (/(token|secret|password|key|credential)/i.test(key)) {
      redacted[key] = "[redacted]";
    } else {
      redacted[key] = entry;
    }
  }
  return truncate(toYaml(redacted), 360);
}

function renderValue(value: unknown): string | undefined {
  if (value === null || value === undefined || value === "") return undefined;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    const rendered = value.map(renderValue).filter(isPresent);
    return rendered.length > 0 ? rendered.join(", ") : undefined;
  }
  return truncate(toYaml(value), 240);
}

function stringValue(value: unknown): string | undefined {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return undefined;
}

function booleanValue(value: unknown): boolean | undefined {
  if (typeof value === "boolean") return value;
  return undefined;
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asRecord(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : null;
}

function addField(fields: ProjectionField[], label: string, value: string | undefined) {
  if (value) fields.push({ label, value });
}

function confidenceValue(value: unknown): string | undefined {
  if (typeof value === "number") return `${Math.round(value * 100)}%`;
  return stringValue(value);
}

function similarityValue(value: unknown): string | undefined {
  if (typeof value === "number") return value.toFixed(2);
  return stringValue(value);
}

function truncate(value: string, limit: number): string | undefined {
  const text = value.trim().replace(/\s+/g, " ");
  if (!text) return undefined;
  return text.length <= limit ? text : `${text.slice(0, Math.max(0, limit - 1)).trim()}...`;
}

function titleize(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function isPresent<T>(value: T | null | undefined): value is T {
  return value !== null && value !== undefined && value !== "";
}

function dedupeEvidence(rows: EvidenceRow[]): EvidenceRow[] {
  const byId = new Map<string, EvidenceRow>();
  for (const row of rows) {
    const existing = byId.get(row.id);
    byId.set(row.id, existing ? { ...existing, ...row, selected: existing.selected || row.selected } : row);
  }
  return Array.from(byId.values());
}
