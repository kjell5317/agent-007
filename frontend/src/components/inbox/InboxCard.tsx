import { useState } from "react";
import { CirclePlus, RotateCcw, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { Markdown } from "@/components/ui/markdown";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import { inboxBadge, inputTitle, isAgentTaskFollowup, senderName } from "@/lib/inbox";
import {
  projectAgentTrace,
  projectSourceMetadata,
  type EvidenceRow,
  type ProjectionField,
  type ToolRow,
} from "@/lib/projections";
import { cn } from "@/lib/utils";
import { useInboxActions } from "@/components/inbox/useInboxActions";
import type { RawInput } from "@/lib/types";

export interface InboxItem {
  id: string;
  sort: string;
  data: RawInput;
}

interface Props {
  item: InboxItem;
  onChanged: () => Promise<void> | void;
  seenAfter: string | null;
}

export function InboxCard({ item, onChanged, seenAfter }: Props) {
  const [open, setOpen] = useState(false);
  const { busy, runTaskAction, promote } = useInboxActions(onChanged);

  const data = item.data;
  const label = inboxBadge(data);
  const title = inputTitle(data);
  const when = fmtWhen(data.received_at);
  // Manual entries are excluded from the inbox unread badge (count_since
  // filters source="manual" — the user just created them, no need to
  // notify themselves). Suppress the per-card dot too so the two stay
  // consistent.
  const unread =
    seenAfter !== null &&
    data.source !== "manual" &&
    new Date(data.received_at).getTime() > new Date(seenAfter).getTime();

  const dismiss = () => {
    if (data.task_id) runTaskAction(data.task_id, api.markNotTask, "Task dismissed");
  };
  const reopen = () => {
    if (data.task_id) runTaskAction(data.task_id, api.reopenTask, "Task re-opened");
  };

  // Promote when the input isn't the anchor of an active task: no link at all,
  // or the link is a marker the user can override into a fresh task — an
  // embedding auto-decided `duplicate` or a `not_task` row. When the *agent*
  // acted on an existing task (reopened / updated / closed / no_change), the
  // task is real and "Make a task" would duplicate it, so it's suppressed.
  // Otherwise: open task → dismiss, closed → reopen.
  const promotable =
    !isAgentTaskFollowup(data) &&
    (!data.task_id ||
      data.status === "duplicate" ||
      data.status === "not_task");
  const action = promotable
    ? { label: "Make a task", Icon: CirclePlus, run: () => promote(item.id) }
    : data.status === "open"
      ? { label: "Dismiss task", Icon: Trash2, run: dismiss }
      : data.status === "closed"
        ? { label: "Re-open task", Icon: RotateCcw, run: reopen }
        : null;

  return (
    <Card>
      <CardContent
        className="cursor-pointer"
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button,a,summary")) return;
          setOpen((v) => !v);
        }}
      >
        <div className="flex items-center gap-2">
          {action ? (
            <ActionButton {...action} disabled={busy} />
          ) : (
            // Keep the leading column reserved so cards align whether or
            // not they have an action button.
            <div className="h-8 w-8 shrink-0" />
          )}

          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              {unread && (
                <span
                  aria-label="Unread"
                  title="Unread"
                  className="inline-block h-2 w-2 shrink-0 rounded-full bg-emerald-500"
                />
              )}
              <div className="min-w-0 flex-1 truncate font-medium leading-snug">
                {title}
              </div>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
              <Badge variant={label}>{label}</Badge>
              <span className="truncate font-medium">{senderName(data)}</span>
              <MetaDot />
              <span className="font-medium">{when}</span>
            </div>
          </div>
        </div>

        <Collapsible open={open}>
          <div
            className="mt-3 space-y-3 border-t pt-3 text-sm"
            onClick={(e) => e.stopPropagation()}
          >
            <InputBody data={data} />
          </div>
        </Collapsible>
      </CardContent>
    </Card>
  );
}

// Separator between meta labels — keeps the gaps legible.
export function MetaDot() {
  return (
    <span aria-hidden className="text-muted-foreground">
      •
    </span>
  );
}

export function ActionButton({
  label,
  Icon,
  run,
  disabled,
}: {
  label: string;
  Icon: typeof CirclePlus;
  run: () => void;
  disabled: boolean;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={run}
      className={cn(
        "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-primary disabled:pointer-events-none disabled:opacity-50",
      )}
    >
      <Icon className="h-5 w-5" />
    </button>
  );
}

export function InputBody({ data }: { data: RawInput }) {
  const metadata = projectSourceMetadata(
    data.source,
    data.external_id,
    data.source_metadata,
  );
  const trace = data.agent_trace ? projectAgentTrace(data.agent_trace) : null;

  return (
    <>
      {metadata.fields.length > 0 && (
        <details className="rounded-md border bg-muted/20 px-2 py-1.5">
          <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
            Metadata
          </summary>
          <div className="mt-2">
            <FieldGrid fields={metadata.fields} />
          </div>
        </details>
      )}
      {data.content && (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">
            content
          </summary>
          <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap break-words">
            {data.content}
          </pre>
        </details>
      )}
      {trace && <TraceView trace={trace} />}
    </>
  );
}

function FieldGrid({ fields }: { fields: ProjectionField[] }) {
  return (
    <div className="grid gap-x-4 gap-y-1 rounded-md border bg-muted/20 p-2 text-xs sm:grid-cols-2">
      {fields.map((field) => (
        <div key={`${field.label}:${field.value}`} className="min-w-0">
          <span className="text-muted-foreground">{field.label}: </span>
          <span className="break-words font-medium">{field.value}</span>
        </div>
      ))}
    </div>
  );
}

function TraceView({ trace }: { trace: ReturnType<typeof projectAgentTrace> }) {
  return (
    <details className="rounded-md border bg-muted/10 px-2 py-1.5">
      <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
        Decision
      </summary>
      <div className="mt-2 space-y-2">
        <FieldGrid fields={trace.summary} />
        {trace.reason && (
          <div className="rounded-md border bg-background p-2">
            <div className="mb-1 text-xs font-medium text-muted-foreground">Reason</div>
            <Markdown content={trace.reason} className="text-xs" />
          </div>
        )}
        {trace.evidence.length > 0 && (
          <div className="space-y-1">
            <div className="text-xs font-medium text-muted-foreground">Precedents</div>
            {trace.evidence.map((row) => (
              <EvidenceItem key={row.id} row={row} />
            ))}
          </div>
        )}
        {trace.tools.length > 0 && (
          <div className="space-y-1">
            <div className="text-xs font-medium text-muted-foreground">Tools</div>
            {trace.tools.map((row) => (
              <ToolItem key={row.id} row={row} />
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

function EvidenceItem({ row }: { row: EvidenceRow }) {
  const meta = [
    row.sender,
    row.receivedAt ? fmtWhen(row.receivedAt) : null,
    row.source,
    row.similarity ? `sim ${row.similarity}` : null,
  ].filter(Boolean);

  return (
    <div
      id={row.id}
      className={cn(
        "rounded-md border bg-background px-2 py-1.5 text-xs",
        row.selected && "border-primary/50 bg-primary/5",
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-medium">{row.title}</span>
        {row.status && (
          <Badge variant={statusBadgeVariant(row.status)} className="shrink-0">
            {row.status}
          </Badge>
        )}
      </div>
      <div className="mt-0.5 break-words text-muted-foreground">{meta.join(" · ")}</div>
    </div>
  );
}

function ToolItem({ row }: { row: ToolRow }) {
  return (
    <details className="rounded-md border bg-background px-2 py-1.5 text-xs">
      <summary className="cursor-pointer list-none">
        <span className="flex min-w-0 items-center gap-2">
          <span
            className={cn(
              "h-2 w-2 shrink-0 rounded-full",
              toolStatusClass(row.status),
            )}
          />
          <span className="min-w-0 flex-1 truncate font-medium">{row.name}</span>
          <span className="shrink-0 text-muted-foreground">{row.status}</span>
          {row.confidence && (
            <span className="shrink-0 text-muted-foreground">
              {row.confidence}
            </span>
          )}
          {row.changedState !== undefined && (
            <span className="shrink-0 text-muted-foreground">
              {row.changedState ? "changed" : "read-only"}
            </span>
          )}
        </span>
      </summary>
      <div className="mt-1 space-y-1 text-muted-foreground">
        <div>{row.purpose}</div>
        {row.input && (
          <pre className="max-h-28 overflow-auto rounded bg-muted p-1.5 whitespace-pre-wrap break-words">
            {row.input}
          </pre>
        )}
        {row.reason && <Markdown content={row.reason} className="text-xs text-foreground" />}
        {row.result && <Markdown content={row.result} className="text-xs text-foreground" />}
        {row.artifacts.length > 0 && (
          <div className="break-words">Artifacts: {row.artifacts.join(", ")}</div>
        )}
      </div>
    </details>
  );
}

function toolStatusClass(status: ToolRow["status"]) {
  if (status === "success") return "bg-emerald-500";
  if (status === "failed" || status === "timed_out") return "bg-red-500";
  if (status === "denied") return "bg-orange-500";
  if (status === "skipped") return "bg-slate-400";
  return "bg-blue-500";
}

function statusBadgeVariant(status: string) {
  if (
    status === "open" ||
    status === "closed" ||
    status === "duplicate" ||
    status === "not_task" ||
    status === "updated" ||
    status === "reopened" ||
    status === "no_change"
  ) {
    return status;
  }
  return "muted";
}
