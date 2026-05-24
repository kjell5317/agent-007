import { useState } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import type { RawInput, Task } from "@/lib/types";

export type InboxItem =
  | { kind: "input"; id: string; sort: string; data: RawInput }
  | { kind: "task"; id: string; sort: string; data: Task };

interface Props {
  item: InboxItem;
  onChanged: () => Promise<void> | void;
}

type BadgeKind = "not_task" | "duplicate" | "no_change" | "closed";

function labelFor(item: InboxItem): BadgeKind {
  if (item.kind === "task") return "closed";
  const outcome = item.data.agent_trace?.outcome;
  if (outcome === "no_change") return "no_change";
  return item.data.status as BadgeKind;
}

export function InboxCard({ item, onChanged }: Props) {
  const [open, setOpen] = useState(false);
  const label = labelFor(item);
  const isInput = item.kind === "input";

  const title = isInput
    ? item.data.source_metadata?.subject ||
      (item.data.content || "").slice(0, 80) ||
      "(no subject)"
    : item.data.title;
  const when = fmtWhen(
    isInput ? item.data.received_at : item.data.updated_at || item.data.created_at,
  );
  const source = isInput ? item.data.source : "task";

  const promote = async () => {
    const title = prompt("Title for the new task?");
    if (!title) return;
    try {
      await api.promoteInput(item.id, title);
      toast.success("Task created");
      await onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  return (
    <Card>
      <CardContent
        className="cursor-pointer"
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button,a,summary")) return;
          setOpen((v) => !v);
        }}
      >
        <div className="font-medium leading-snug">{title}</div>
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <Badge variant={label}>{label}</Badge>
          <span>{source}</span>
          <span>{when}</span>
        </div>

        <Collapsible open={open}>
          <div className="mt-3 space-y-3 border-t pt-3 text-sm" onClick={(e) => e.stopPropagation()}>
            {isInput ? <InputBody data={item.data} onPromote={promote} /> : <TaskBody data={item.data} />}
          </div>
        </Collapsible>
      </CardContent>
    </Card>
  );
}

function InputBody({ data, onPromote }: { data: RawInput; onPromote: () => void }) {
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
          <summary className="cursor-pointer text-xs text-muted-foreground">metadata</summary>
          <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap break-words">
            {JSON.stringify(data.source_metadata, null, 2)}
          </pre>
        </details>
      )}
      {data.content && (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">content</summary>
          <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap break-words">
            {data.content}
          </pre>
        </details>
      )}
      {data.agent_trace && (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">agent trace</summary>
          <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap break-words">
            {JSON.stringify(data.agent_trace, null, 2)}
          </pre>
        </details>
      )}
      <div>
        <Button size="sm" onClick={onPromote}>
          Promote to task
        </Button>
      </div>
    </>
  );
}

function TaskBody({ data }: { data: Task }) {
  return (
    <>
      {data.description && (
        <div>
          <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Description
          </div>
          <div>{data.description}</div>
        </div>
      )}
      {data.due_date && (
        <div>
          <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Due
          </div>
          <div>{fmtWhen(data.due_date)}</div>
        </div>
      )}
      {data.location && (
        <div>
          <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Location
          </div>
          <div>{data.location}</div>
        </div>
      )}
      {data.link && (
        <div>
          <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Link
          </div>
          <a
            href={data.link}
            target="_blank"
            rel="noopener"
            className="text-primary underline"
          >
            {data.link}
          </a>
        </div>
      )}
    </>
  );
}
