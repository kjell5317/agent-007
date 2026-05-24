import { useMemo } from "react";
import { InboxCard, type InboxItem } from "@/components/InboxCard";
import type { RawInput, Task } from "@/lib/types";

interface Props {
  inputs: RawInput[];
  closedTasks: Task[];
  onChanged: () => Promise<void> | void;
}

export function InboxPanel({ inputs, closedTasks, onChanged }: Props) {
  const items = useMemo<InboxItem[]>(() => {
    const inputItems: InboxItem[] = inputs
      .filter((r) => {
        if (r.status === "not_task" || r.status === "duplicate") return true;
        return r.agent_trace?.outcome === "no_change";
      })
      .map((r) => ({ kind: "input", id: r.id, sort: r.received_at, data: r }));

    const taskItems: InboxItem[] = closedTasks.map((t) => ({
      kind: "task",
      id: t.id,
      sort: t.updated_at || t.created_at,
      data: t,
    }));

    return [...inputItems, ...taskItems].sort(
      (a, b) => new Date(b.sort).getTime() - new Date(a.sort).getTime(),
    );
  }, [inputs, closedTasks]);

  if (items.length === 0) {
    return (
      <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
        Inbox is empty.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {items.map((it) => (
        <InboxCard key={`${it.kind}:${it.id}`} item={it} onChanged={onChanged} />
      ))}
    </div>
  );
}
