import { useState } from "react";
import { CirclePlus, RotateCcw, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import { inboxBadge, inputTitle, senderName } from "@/lib/inbox";
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

  // Promote when the input isn't the anchor of an active task: no link at
  // all, or the link is a marker the user can override into a fresh task
  // (`duplicate`, a `no_change` follow-up, or a `not_task` row whose
  // task_id is still set from a pre-fix dismiss). Otherwise: open task →
  // dismiss, closed task → reopen, anything else → no action.
  const traceOutcome = data.agent_trace?.outcome;
  const promotable =
    !data.task_id ||
    data.status === "duplicate" ||
    data.status === "not_task" ||
    traceOutcome === "no_change";
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
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <Badge variant={label}>{label}</Badge>
              <span className="truncate">{senderName(data)}</span>
              <span>{when}</span>
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
  return (
    <>
      <div className="text-xs text-muted-foreground">
        Source: {data.source}
        {data.external_id ? (
          <>
            {" · "}
            <code className="font-mono">{data.external_id}</code>
          </>
        ) : null}
      </div>
      {data.source_metadata && Object.keys(data.source_metadata).length > 0 && (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">
            metadata
          </summary>
          <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap break-words">
            {JSON.stringify(data.source_metadata, null, 2)}
          </pre>
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
      {data.agent_trace && (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">
            agent trace
          </summary>
          <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap break-words">
            {JSON.stringify(data.agent_trace, null, 2)}
          </pre>
        </details>
      )}
    </>
  );
}
