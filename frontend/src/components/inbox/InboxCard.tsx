import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ChevronDown,
  ChevronRight,
  CirclePlus,
  Gauge,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { Markdown } from "@/components/ui/markdown";
import { InputStatusBadge } from "@/components/runs/RunStatusBadge";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import {
  inputTitle,
  isAgentTaskFollowup,
  isDismissibleKotxRun,
  isKotxRun,
  senderName,
  badgeKindLabel,
} from "@/lib/inbox";
import {
  projectAgentTrace,
  type EvidenceRow,
  type ProjectionField,
  type ToolRow,
} from "@/lib/projections";
import { cn } from "@/lib/utils";
import { useInboxActions } from "@/components/inbox/useInboxActions";
import { useResolvedEvidence } from "@/hooks/useResolvedEvidence";
import type { RawInput } from "@/lib/types";

const NO_EVIDENCE: EvidenceRow[] = [];

export interface InboxItem {
  id: string;
  sort: string;
  data: RawInput;
}

interface Props {
  item: InboxItem;
  onChanged: () => Promise<void> | void;
  unseen: boolean;
  onVisible: (id: string) => void;
  onOpenTask: (id: string) => void;
}

export function InboxCard({
  item,
  onChanged,
  unseen,
  onVisible,
  onOpenTask,
}: Props) {
  const [open, setOpen] = useState(false);
  const cardRef = useRef<HTMLDivElement>(null);
  const { busy, runTaskAction, promote, reopenTask, dismissRun } =
    useInboxActions(onChanged);

  const data = item.data;
  const title = inputTitle(data);
  const when = fmtWhen(data.received_at);
  const kotxRun = isKotxRun(data);
  const cardBorderClass = kotxRun
    ? "border-primary/50"
    : unseen
      ? "border-emerald-500/70"
      : null;

  useEffect(() => {
    if (!unseen) return;
    const node = cardRef.current;
    if (!node) return;

    if (typeof IntersectionObserver === "undefined") {
      onVisible(item.id);
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        onVisible(item.id);
        observer.disconnect();
      },
      { threshold: 0.5 },
    );

    observer.observe(node);
    return () => observer.disconnect();
  }, [item.id, onVisible, unseen]);

  const dismiss = () => {
    if (data.task_id)
      runTaskAction(data.task_id, api.markNotTask, "Task dismissed");
  };
  const reopen = () => {
    if (data.task_id) reopenTask(data.task_id);
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
  // A promotable kotx card is a run that hasn't produced a task yet (preparing/
  // queued/running). Don't offer "Make a task" — kotx makes the task itself
  // when the run reaches an actionable state. Instead let the user dismiss the
  // run (discard it upstream); an already-terminal run gets no action.
  const action = promotable
    ? isKotxRun(data)
      ? isDismissibleKotxRun(data)
        ? { label: "Dismiss run", Icon: Trash2, run: () => dismissRun(data.id) }
        : null
      : { label: "Make a task", Icon: CirclePlus, run: () => promote(item.id) }
    : data.status === "open"
      ? { label: "Dismiss task", Icon: Trash2, run: dismiss }
      : data.status === "closed"
        ? { label: "Re-open task", Icon: RotateCcw, run: reopen }
        : null;

  const expandable = !data.task_id && hasInputDetails(data);

  // Linked inputs open their task. Unlinked inputs use the card body as their
  // details toggle when there is anything to show.
  return (
    <Card ref={cardRef} className={cn(cardBorderClass)}>
      <CardContent
        className={cn((data.task_id || expandable) && "cursor-pointer")}
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button,a,summary")) return;
          if (data.task_id) {
            onOpenTask(data.task_id);
            return;
          }
          if (!expandable) return;
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
              <div className="min-w-0 flex-1 truncate font-medium leading-snug">
                {title}
              </div>
            </div>
            <div className="mt-1 flex min-w-0 items-center gap-2 overflow-hidden text-xs text-muted-foreground">
              <span className="shrink-0">
                <InputStatusBadge input={data} />
              </span>
              <span className="min-w-0 flex-1 truncate font-medium">
                {senderName(data)}
              </span>
              <span className="shrink-0 font-medium">{when}</span>
            </div>
          </div>

          <div aria-hidden className="h-6 w-6 shrink-0" />
        </div>

        {expandable && (
          <Collapsible open={open}>
            <div
              className="mt-3 space-y-3 border-t pt-3 text-sm"
              onClick={(e) => e.stopPropagation()}
            >
              <InputBody data={data} />
            </div>
          </Collapsible>
        )}
      </CardContent>
    </Card>
  );
}

// Separator between meta labels — keeps the gaps legible.
export function MetaDot() {
  return (
    <span aria-hidden className="shrink-0 text-muted-foreground">
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

// Mirrors InputBody's render conditions — cards skip the dropdown (and the
// modal skips the body block) when nothing would show.
export function hasInputDetails(data: RawInput): boolean {
  if (hasSourceContentDetails(data)) return true;
  const trace = data.agent_trace ? projectAgentTrace(data.agent_trace) : null;
  if (!trace) return false;
  return (
    Boolean(trace.reason) ||
    trace.currentTask.length > 0 ||
    trace.evidence.length > 0 ||
    trace.tools.length > 0
  );
}

export function InputBody({ data }: { data: RawInput }) {
  const trace = data.agent_trace ? projectAgentTrace(data.agent_trace) : null;
  const evidence = useResolvedEvidence(trace?.evidence ?? NO_EVIDENCE);

  return (
    <>
      {hasSourceContentDetails(data) && (
        <Section title="Source content">
          <div className="max-h-60 overflow-y-auto break-words text-xs">
            <Markdown content={data.content} className="text-xs" />
          </div>
        </Section>
      )}
      {trace?.reason && (
        <Section title="Reason">
          <Markdown content={trace.reason} className="text-xs" />
        </Section>
      )}
      {trace &&
        (trace.currentTask.length > 0 ||
          evidence.length > 0 ||
          trace.tools.length > 0) && (
          <CollapsibleSection title="Agent trace">
            {trace.currentTask.length > 0 && (
              <Section title="Current task">
                <FieldGrid fields={trace.currentTask} />
              </Section>
            )}
            {evidence.length > 0 && (
              <Section title="Precedents">
                <div className="space-y-1">
                  {evidence.map((row) => (
                    <EvidenceItem key={row.id} row={row} />
                  ))}
                </div>
              </Section>
            )}
            {trace.tools.length > 0 && (
              <Section title="Tool calls">
                <div className="space-y-1">
                  {trace.tools.map((row) => (
                    <ToolItem key={row.id} row={row} />
                  ))}
                </div>
              </Section>
            )}
          </CollapsibleSection>
        )}
    </>
  );
}

function hasSourceContentDetails(data: RawInput): boolean {
  return data.source !== "kotx" && Boolean(data.content);
}

function SectionLabel({ title }: { title: string }) {
  return (
    <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
      {title}
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-1">
      <SectionLabel title={title} />
      {children}
    </section>
  );
}

function CollapsibleSection({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <section className="space-y-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 text-left"
      >
        <span className="min-w-0 flex-1">
          <SectionLabel title={title} />
        </span>
        <Chevron className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      </button>
      <Collapsible open={open}>
        <div className="space-y-3">{children}</div>
      </Collapsible>
    </section>
  );
}

function FieldGrid({ fields }: { fields: ProjectionField[] }) {
  return (
    <div className="grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
      {fields.map((field) => (
        <div key={`${field.label}:${field.value}`} className="min-w-0">
          <span className="text-muted-foreground">{field.label}: </span>
          <span className="whitespace-pre-wrap break-words font-medium">
            {field.value}
          </span>
        </div>
      ))}
    </div>
  );
}

function EvidenceItem({ row }: { row: EvidenceRow }) {
  const when = row.receivedAt ? fmtWhen(row.receivedAt) : null;

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
            {badgeKindLabel(row.status)}
          </Badge>
        )}
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-muted-foreground">
        {row.sender && (
          <span className="truncate font-medium">{row.sender}</span>
        )}
        {row.sender && when && <MetaDot />}
        {when && <span className="font-medium">{when}</span>}
        {(row.sender || when) && row.label && <MetaDot />}
        {row.label && <span className="font-medium">{row.label}</span>}
        {(row.sender || when || row.label) && row.source && <MetaDot />}
        {row.source && <span className="font-medium">{row.source}</span>}
        {row.similarity && (row.sender || when || row.label || row.source) && <MetaDot />}
        {row.similarity && (
          <span
            className="inline-flex items-center gap-1 font-medium"
            title="Similarity"
          >
            <Gauge className="h-3 w-3" aria-hidden />
            {row.similarity}
          </span>
        )}
      </div>
    </div>
  );
}

function ToolItem({ row }: { row: ToolRow }) {
  const [open, setOpen] = useState(false);
  const hasDetails = Boolean(
    (row.inputFields && row.inputFields.length > 0) || row.reason || row.result,
  );
  const Chevron = open ? ChevronDown : ChevronRight;

  const header = (
    <>
      <span
        className={cn(
          "h-2 w-2 shrink-0 rounded-full",
          toolStatusClass(row.status),
        )}
      />
      <span className="min-w-0 flex-1 truncate font-medium">{row.purpose}</span>
      {row.confidence && (
        <span className="shrink-0 tabular-nums text-muted-foreground">
          {row.confidence}
        </span>
      )}
      {row.status !== "success" && (
        <span className="shrink-0 text-muted-foreground">
          {row.status.replace("_", " ")}
        </span>
      )}
    </>
  );

  if (!hasDetails) {
    return (
      <div className="flex items-center gap-2 rounded-md border bg-background px-2 py-1.5 text-xs">
        {header}
      </div>
    );
  }

  return (
    <div className="rounded-md border bg-background text-xs">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
      >
        {header}
        <Chevron className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      </button>
      <Collapsible open={open}>
        <div className="space-y-2 border-t px-2 pb-2 pt-2">
          {row.inputFields && row.inputFields.length > 0 && (
            <Section title="Input">
              <FieldGrid fields={row.inputFields} />
            </Section>
          )}
          {row.reason && (
            <Section title="Reason">
              <Markdown content={row.reason} className="text-xs" />
            </Section>
          )}
          {row.result && (
            <Section title="Result">
              <Markdown content={row.result} className="text-xs" />
            </Section>
          )}
        </div>
      </Collapsible>
    </div>
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
