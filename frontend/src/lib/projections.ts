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
  label?: string;
  sender?: string;
  receivedAt?: string;
  selected?: boolean;
  aggregate?: boolean;
}

export interface ToolRow {
  id: string;
  name: string;
  status: "success" | "failed" | "skipped" | "denied" | "timed_out" | "called";
  purpose: string;
  inputFields?: ProjectionField[];
  result?: string;
  reason?: string;
  confidence?: string;
}

export interface TraceProjection {
  summary: ProjectionField[];
  currentTask: ProjectionField[];
  reason?: string;
  confidence?: string;
  evidence: EvidenceRow[];
  tools: ToolRow[];
}

export interface MetadataProjection {
  fields: ProjectionField[];
}

export interface LogRow {
  id: string;
  kind: "decision" | "tool" | "action" | "event";
  indicator: string;
  recordId?: string;
  status: string;
  title: string;
  subtitle?: string;
  body?: string;
  bodyFormat?: "markdown" | "yaml" | "text";
  tokens: ProjectionField[];
}

export interface KotxLogProjection {
  rows: LogRow[];
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
  const hiddenKeys = hiddenMetadataKeys(source);
  if (externalId && !hiddenKeys.has("external_id")) {
    fields.push({ label: "External id", value: externalId });
  }

  const seen = new Set<string>();
  const meta = metadata ?? {};
  for (const [key, label] of METADATA_KEYS) {
    if (hiddenKeys.has(key)) continue;
    if (seen.has(label)) continue;
    const rendered = renderValue(meta[key]);
    if (!rendered) continue;
    seen.add(label);
    fields.push({ label, value: rendered });
  }

  return { fields };
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
    stringValue(record.task_title) ??
      stringValue(record.title) ??
      stringValue(record.task_id) ??
      stringValue(record.existing_task_id),
  );
  if (selected) addField(summary, "Selected evidence", selected.title);
  const reason = stringValue(record.reason);
  const confidence = confidenceValue(record.confidence);
  addField(summary, "Confidence", confidence);

  if (summary.length === 0) {
    summary.push({ label: "Trace", value: "No decision summary was recorded." });
  }

  return {
    summary,
    currentTask: currentTaskFields(record),
    reason,
    confidence,
    evidence,
    tools,
  };
}

function currentTaskFields(trace: JsonRecord): ProjectionField[] {
  const task = asRecord(trace.current_task);
  if (!task) return [];

  const fields: ProjectionField[] = [];
  addField(fields, "title", stringValue(task.title));
  addField(fields, "description", stringValue(task.description));
  addField(fields, "due_date", stringValue(task.due_date));
  addField(fields, "scheduled_date", stringValue(task.scheduled_date));
  addField(fields, "estimation", estimationValue(task.estimation));
  addField(fields, "location", stringValue(task.location));
  addField(fields, "link", stringValue(task.link));
  addField(fields, "label", stringValue(task.label));
  return fields;
}

export function projectKotxLog(text: string | null): KotxLogProjection {
  if (!text?.trim()) {
    return { rows: [], rawFallback: "", infrastructureCount: 0 };
  }

  const records = parseJsonRecords(text);
  if (records.length === 0) {
    return {
      rows: [],
      rawFallback: text,
      infrastructureCount: 0,
    };
  }

  const rows: LogRow[] = [];
  let infrastructureCount = 0;

  records.forEach((record, index) => {
    const row = logRow(record, index);
    if (row && !isInfrastructureLog(record)) {
      rows.push(row);
    } else {
      infrastructureCount += 1;
    }
  });

  return {
    rows,
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
  const title = stringValue(record.title) ?? stringValue(record.subject) ?? "";

  return {
    id,
    kind: stringValue(record.kind) === "precedent" ? "precedent" : kind,
    title,
    status: stringValue(record.status),
    source: stringValue(record.source),
    similarity: similarityValue(record.similarity ?? record.sim),
    taskId: stringValue(record.task_id),
    label: stringValue(record.label),
    sender: senderDisplayName(stringValue(record.sender) ?? stringValue(record.from)),
    receivedAt: stringValue(record.received_at),
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
    const resultReason =
      stringValue(result?.reason) ??
      stringValue(input?.reason) ??
      stringValue(result?.result_reason);
    const display = toolDisplay(name, input ?? asRecord(block.input));
    return [
      {
        id: stringValue(block.id) ?? `${prefix}-tool-${index + 1}`,
        name,
        status: normalizeToolStatus(result),
        purpose: stringValue(result?.purpose) ?? toolPurpose(name, input),
        inputFields:
          display.inputFields ??
          (display.hideInput ? undefined : genericInputFields(input)),
        result: display.hideResult
          ? undefined
          : stringValue(result?.result_markdown) ??
            stringValue(result?.result) ??
            stringValue(result?.output) ??
            stringValue(result?.result_summary) ??
            stringValue(result?.preview),
        reason: display.hideReason ? undefined : resultReason,
        confidence: display.hideConfidence
          ? undefined
          : confidenceValue(result?.confidence ?? input?.confidence),
      },
    ];
  });
}

function logRow(record: JsonRecord, index: number): LogRow | null {
  const indicator =
    stringValue(record.type) ??
    stringValue(record.kind) ??
    stringValue(record.event) ??
    "event";
  const recordId =
    stringValue(record.id) ??
    stringValue(record.record_id) ??
    stringValue(record.item_id) ??
    stringValue(record.run_item_id) ??
    stringValue(record.call_id);
  const message =
    stringValue(record.message) ??
    stringValue(record.msg) ??
    stringValue(record.event) ??
    stringValue(record.type);
  const event = `${stringValue(record.event) ?? ""} ${stringValue(record.type) ?? ""} ${stringValue(record.kind) ?? ""}`.toLowerCase();
  const toolName =
    stringValue(record.tool) ??
    stringValue(record.tool_name) ??
    stringValue(asRecord(record.tool_call)?.name);
  const status =
    stringValue(record.status) ??
    stringValue(record.level) ??
    stringValue(record.outcome) ??
    "event";
  const payload =
    asRecord(record.payload) ??
    asRecord(record.item) ??
    asRecord(record.data) ??
    asRecord(record.body);
  const title =
    stringValue(record.title) ??
    stringValue(payload?.title) ??
    stringValue(payload?.name) ??
    stringValue(payload?.summary);
  const tokens = tokenFields(record);

  if (event.includes("item_started")) {
    return {
      id: `log-${index + 1}`,
      kind: "event",
      indicator,
      recordId,
      status,
      title: title ?? nonRepeatedMessage(message, indicator) ?? "Item started",
      body: toYaml(payload ?? displayRecord(record)),
      bodyFormat: "yaml",
      tokens,
    };
  }

  if (event.includes("item_completed")) {
    const text =
      stringValue(record.text) ??
      stringValue(record.output) ??
      stringValue(record.result) ??
      stringValue(payload?.text) ??
      stringValue(payload?.output) ??
      stringValue(payload?.result) ??
      nonRepeatedMessage(message, indicator);
    return {
      id: `log-${index + 1}`,
      kind: "event",
      indicator,
      recordId,
      status,
      title: title ?? "Item completed",
      body: text,
      bodyFormat: "markdown",
      tokens,
    };
  }

  if (toolName || event.includes("tool")) {
    return {
      id: `log-${index + 1}`,
      kind: "tool",
      indicator,
      recordId,
      status,
      title: toolName ?? nonRepeatedMessage(message, indicator) ?? "Tool call",
      subtitle: message && toolName ? nonRepeatedMessage(message, indicator) : undefined,
      body: truncate(
        stringValue(record.result_markdown) ??
          stringValue(record.result) ??
          stringValue(record.output) ??
          stringValue(record.preview) ??
          "",
        280,
      ),
      bodyFormat: "markdown",
      tokens,
    };
  }

  if (event.includes("decision") || event.includes("outcome") || record.outcome) {
    return {
      id: `log-${index + 1}`,
      kind: "decision",
      indicator,
      recordId,
      status,
      title: nonRepeatedMessage(message, indicator) ?? stringValue(record.outcome) ?? "Decision",
      body: truncate(stringValue(record.reason) ?? "", 280),
      bodyFormat: "markdown",
      tokens,
    };
  }

  if (event.includes("action") || event.includes("state") || record.action) {
    return {
      id: `log-${index + 1}`,
      kind: "action",
      indicator,
      recordId,
      status,
      title: nonRepeatedMessage(message, indicator) ?? stringValue(record.action) ?? "Action",
      body: truncate(stringValue(record.detail) ?? stringValue(record.reason) ?? "", 280),
      bodyFormat: "markdown",
      tokens,
    };
  }

  if (message) {
    return {
      id: `log-${index + 1}`,
      kind: "event",
      indicator,
      recordId,
      status,
      title: nonRepeatedMessage(message, indicator) ?? message,
      body: payload ? toYaml(payload) : undefined,
      bodyFormat: payload ? "yaml" : undefined,
      tokens,
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
  const eventName = `${stringValue(record.event) ?? ""} ${stringValue(record.type) ?? ""} ${stringValue(record.kind) ?? ""}`.toLowerCase();
  if (eventName.includes("item_started") || eventName.includes("item_completed")) {
    return false;
  }
  if (tokenFields(record).length > 0) {
    return false;
  }
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

function hiddenMetadataKeys(source: string): Set<string> {
  const normalized = source.toLowerCase();
  if (normalized === "gmail") {
    return new Set([
      "external_id",
      "subject",
      "from",
      "sender",
      "date",
      "received_at",
      "received_date",
      "account",
    ]);
  }
  if (normalized === "slack") {
    return new Set(["external_id", "account"]);
  }
  return new Set();
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
  if (name === "update_event") return "Update calendar event";
  if (name === "create_task") return `Create task "${stringValue(input?.title) ?? "task"}"`;
  if (name === "update_task") return "Update existing task";
  if (name === "mark_not_task") return "Mark as not a task";
  if (name === "no_change") return "Leave existing task unchanged";
  return titleize(name);
}

function toolDisplay(
  name: string,
  input: JsonRecord | null | undefined,
): {
  inputFields?: ProjectionField[];
  hideInput?: boolean;
  hideReason?: boolean;
  hideResult?: boolean;
  hideConfidence?: boolean;
} {
  // Terminal decision tools carry reason/confidence, but the decision's rationale
  // is surfaced once at the trace level (see InputBody's "Reason" section), so the
  // per-tool copy is hidden here to avoid showing it twice.
  if (name === "create_task") {
    if (!input) return {};
    const fields = createTaskFields(input);
    return {
      inputFields: fields.length > 0 ? fields : undefined,
      hideInput: true,
      hideReason: true,
      hideResult: true,
      hideConfidence: true,
    };
  }

  if (name === "mark_not_task") {
    if (!input) return {};
    const fields = markNotTaskFields(input);
    return {
      inputFields: fields.length > 0 ? fields : undefined,
      hideInput: true,
      hideReason: true,
      hideResult: true,
      hideConfidence: true,
    };
  }

  if (name === "update_task") {
    const fields = input ? updateTaskFields(input) : [];
    return {
      inputFields: fields.length > 0 ? fields : undefined,
      hideInput: true,
      hideReason: true,
      hideResult: true,
      hideConfidence: true,
    };
  }

  return {};
}

function genericInputFields(input: JsonRecord | null | undefined): ProjectionField[] | undefined {
  if (!input) return undefined;
  const fields: ProjectionField[] = [];
  for (const [key, entry] of Object.entries(input)) {
    const value = /(token|secret|password|key|credential)/i.test(key)
      ? "[redacted]"
      : renderValue(entry);
    if (!value) continue;
    fields.push({ label: key, value: truncate(value, 200) ?? value });
  }
  return fields.length > 0 ? fields : undefined;
}

function createTaskFields(input: JsonRecord): ProjectionField[] {
  const fields: ProjectionField[] = [];
  addField(fields, "title", stringValue(input.title));
  addField(fields, "description", stringValue(input.description));
  addField(fields, "estimation", estimationValue(input.estimation));
  addField(fields, "due_date", stringValue(input.due_date));
  addField(fields, "location", stringValue(input.location));
  addField(fields, "link", stringValue(input.link));
  addField(fields, "label", stringValue(input.label));
  return fields;
}

function markNotTaskFields(input: JsonRecord): ProjectionField[] {
  const fields: ProjectionField[] = [];
  const notes = arrayValue(input.notes).map(renderValue).filter(isPresent);
  if (notes.length > 0) {
    fields.push({ label: "notes", value: notes.join("\n") });
  }
  addField(fields, "text", stringValue(input.text) ?? stringValue(input.input));
  return fields;
}

function updateTaskFields(input: JsonRecord): ProjectionField[] {
  const fields: ProjectionField[] = [];
  addField(fields, "status", stringValue(input.status));
  addField(fields, "title", stringValue(input.title));
  addField(fields, "description", stringValue(input.description));
  addField(fields, "estimation", estimationValue(input.estimation));
  addField(fields, "due_date", stringValue(input.due_date));
  addField(fields, "location", stringValue(input.location));
  addField(fields, "link", stringValue(input.link));
  addField(fields, "label", stringValue(input.label));
  return fields;
}

export function aggregateUntitledEvidence(rows: EvidenceRow[]): EvidenceRow[] {
  const visible: EvidenceRow[] = [];
  const groups = new Map<string, EvidenceRow[]>();

  for (const row of rows) {
    if (isUsableEvidenceTitle(row.title)) {
      visible.push(row);
      continue;
    }
    const label = row.status || row.kind;
    groups.set(label, [...(groups.get(label) ?? []), row]);
  }

  for (const [label, group] of groups) {
    const similarities = group
      .map((row) => Number(row.similarity))
      .filter((value) => Number.isFinite(value));
    const avg =
      similarities.length > 0
        ? similarities.reduce((total, value) => total + value, 0) / similarities.length
        : null;
    const count = group.length;
    visible.push({
      id: `aggregate:${label}`,
      kind: group.some((row) => row.kind === "precedent") ? "precedent" : "candidate",
      title: count === 1 ? "1 similar input" : `${count} similar inputs`,
      status: group[0]?.status,
      source: sameString(group.map((row) => row.source)),
      label: sameString(group.map((row) => row.label)),
      similarity: avg === null ? undefined : avg.toFixed(2),
      selected: group.some((row) => row.selected),
      aggregate: true,
    });
  }

  return visible;
}

export function isUsableEvidenceTitle(title: string): boolean {
  const normalized = title.trim().toLowerCase();
  return Boolean(
    normalized &&
      normalized !== "(untitled evidence)" &&
      normalized !== "(no subject)" &&
      normalized !== "untitled" &&
      normalized !== "no subject",
  );
}

function sameString(values: Array<string | undefined>): string | undefined {
  const present = values.filter(isPresent);
  if (present.length === 0) return undefined;
  return present.every((value) => value === present[0]) ? present[0] : undefined;
}

function tokenFields(record: JsonRecord): ProjectionField[] {
  const usage = asRecord(record.usage) ?? asRecord(asRecord(record.payload)?.usage);
  const input =
    stringValue(record.input_tokens) ??
    stringValue(record.prompt_tokens) ??
    stringValue(usage?.input_tokens) ??
    stringValue(usage?.prompt_tokens);
  const output =
    stringValue(record.output_tokens) ??
    stringValue(record.completion_tokens) ??
    stringValue(usage?.output_tokens) ??
    stringValue(usage?.completion_tokens);
  const fields: ProjectionField[] = [];
  addField(fields, "Input tokens", input);
  addField(fields, "Output tokens", output);
  return fields;
}

function nonRepeatedMessage(message: string | undefined, indicator: string): string | undefined {
  if (!message) return undefined;
  return message.toLowerCase() === indicator.toLowerCase() ? undefined : message;
}

function displayRecord(record: JsonRecord): JsonRecord {
  const hidden = new Set(["event", "type", "kind", "id", "record_id", "status", "level"]);
  return Object.fromEntries(Object.entries(record).filter(([key]) => !hidden.has(key)));
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

function estimationValue(value: unknown): string | undefined {
  if (typeof value === "number") return `${value} min`;
  const text = stringValue(value);
  return text ? `${text}${/min\b/i.test(text) ? "" : " min"}` : undefined;
}

function senderDisplayName(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const match = value.match(/^"?([^"<]*?)"?\s*<([^>]+)>$/);
  const name = match ? match[1].trim() || match[2].trim() : value;
  return name.replace(/\s*\([^)]*\)\s*$/, "").trim() || name;
}

function stringValue(value: unknown): string | undefined {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
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
