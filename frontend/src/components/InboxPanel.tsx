import { useMemo, useState } from "react";
import { InboxCard, type InboxItem } from "@/components/InboxCard";
import { Button } from "@/components/ui/button";
import type { RawInput, Task } from "@/lib/types";

interface Props {
  inputs: RawInput[];
  closedTasks: Task[];
  onChanged: () => Promise<void> | void;
  onLoadMore: () => Promise<void>;
  hasMore: boolean;
}

export function InboxPanel({ inputs, closedTasks, onChanged, onLoadMore, hasMore }: Props) {
  const [loadingMore, setLoadingMore] = useState(false);

  const items = useMemo<InboxItem[]>(() => {
    const inputItems: InboxItem[] = inputs
      .filter((r) => {
        if (r.status === "not_task" || r.status === "duplicate") return true;
        return r.agent_trace?.outcome === "no_change";
      })
      .map((r) => ({
        kind: "input",
        id: r.id,
        // sort by "last updated": the agent's decision time when set, else
        // the intake time as a fallback (e.g. still-processing rows).
        sort: r.processed_at || r.received_at,
        data: r,
      }));

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

  const handleLoadMore = async () => {
    setLoadingMore(true);
    try {
      await onLoadMore();
    } finally {
      setLoadingMore(false);
    }
  };

  const loadMoreButton = hasMore ? (
    <div className="flex justify-center pt-2">
      <Button
        variant="outline"
        size="sm"
        onClick={handleLoadMore}
        disabled={loadingMore}
      >
        {loadingMore ? "Loading…" : "Load more"}
      </Button>
    </div>
  ) : null;

  if (items.length === 0) {
    return (
      <div className="space-y-2">
        <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
          Inbox is empty.
        </div>
        {loadMoreButton}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {items.map((it) => (
        <InboxCard key={`${it.kind}:${it.id}`} item={it} onChanged={onChanged} />
      ))}
      {loadMoreButton}
    </div>
  );
}
