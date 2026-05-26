import { useEffect, useRef, useState } from "react";
import { CirclePlus, RotateCcw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import { pollTaskCreation, type PollHandle } from "@/lib/pollTask";
import { cn } from "@/lib/utils";
import type { RawInput } from "@/lib/types";

export interface InboxItem {
  id: string;
  sort: string;
  data: RawInput;
}

type BadgeKind =
  | "open"
  | "not_task"
  | "duplicate"
  | "no_change"
  | "closed";

interface Props {
  item: InboxItem;
  onChanged: () => Promise<void> | void;
}

function labelFor(item: InboxItem): BadgeKind {
  const outcome = item.data.agent_trace?.outcome;
  if (outcome === "no_change") return "no_change";
  return item.data.status as BadgeKind;
}

export function InboxCard({ item, onChanged }: Props) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  // Promote is async on the backend (queue + LLM extract) — track the in-flight
  // poll so we can cancel it if the card unmounts mid-flight.
  const activePolls = useRef<Set<PollHandle>>(new Set());

  useEffect(
    () => () => {
      activePolls.current.forEach((handle) => handle.cancel());
      activePolls.current.clear();
    },
    [],
  );

  const label = labelFor(item);
  const data = item.data;

  // Prefer the linked task's title when we have a live (open) or completed
  // (closed) task attached — that's the human-meaningful name. Fall back to
  // the raw envelope (email subject / content snippet) for everything else.
  const linkedTitle =
    data.task_title && (data.status === "open" || data.status === "closed")
      ? data.task_title
      : null;
  const title =
    linkedTitle ||
    data.source_metadata?.subject ||
    (data.content || "").slice(0, 80) ||
    "(no subject)";
  const when = fmtWhen(data.received_at);
  const source = data.source;

  async function runTaskAction(
    call: (id: string) => Promise<unknown>,
    successMsg: string,
  ) {
    if (!data.task_id) return;
    const taskId = data.task_id;
    setBusy(true);
    try {
      await call(taskId);
      toast.success(successMsg);
      await onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const dismiss = () => runTaskAction(api.markNotTask, "Task dismissed");
  const reopen = () => runTaskAction(api.reopenTask, "Task re-opened");

  async function promote() {
    setBusy(true);
    const toastId = toast.loading("Creating task…", { duration: Infinity });
    let handle: PollHandle | null = null;
    const finish = (run: () => void) => {
      toast.dismiss(toastId);
      run();
      if (handle) activePolls.current.delete(handle);
      setBusy(false);
    };
    try {
      const { raw_input_id } = await api.promoteInput(item.id);
      handle = pollTaskCreation(raw_input_id, {
        onSuccess: () =>
          finish(() => {
            toast.success("Task added");
            void onChanged();
          }),
        onFailure: (message) => finish(() => toast.error(message)),
        onTimeout: () =>
          finish(() => toast.error("Task is taking longer than expected")),
      });
      activePolls.current.add(handle);
    } catch (err) {
      toast.dismiss(toastId);
      toast.error((err as Error).message);
      setBusy(false);
    }
  }

  // No task yet → promote. Open task → dismiss (mark not_task). Closed task
  // → reopen. Other statuses (not_task / duplicate / processing) get no
  // action button.
  const action = !data.task_id
    ? { label: "Make a task", Icon: CirclePlus, run: promote }
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
            <button
              type="button"
              aria-label={action.label}
              title={action.label}
              disabled={busy}
              onClick={action.run}
              className={cn(
                "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-primary disabled:pointer-events-none disabled:opacity-50",
              )}
            >
              <action.Icon className="h-5 w-5" />
            </button>
          ) : (
            // Keep the leading column reserved so cards align whether or
            // not they have an action button.
            <div className="h-8 w-8 shrink-0" />
          )}

          <div className="min-w-0 flex-1">
            <div className="truncate font-medium leading-snug">{title}</div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <Badge variant={label}>{label}</Badge>
              <span>{source}</span>
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

function InputBody({ data }: { data: RawInput }) {
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
