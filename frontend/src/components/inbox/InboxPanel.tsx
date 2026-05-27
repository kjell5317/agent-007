import { useMemo, useState } from "react";
import { InboxCard, type InboxItem } from "@/components/inbox/InboxCard";
import { Button } from "@/components/ui/button";
import type { RawInput } from "@/lib/types";

interface Props {
  inputs: RawInput[];
  onChanged: () => Promise<void> | void;
  onLoadMore: () => Promise<void>;
  hasMore: boolean;
  seenAfter: string | null;
}

export function InboxPanel({
  inputs,
  onChanged,
  onLoadMore,
  hasMore,
  seenAfter,
}: Props) {
  const [loadingMore, setLoadingMore] = useState(false);

  // Inbox is a raw-input log/debug view. Tasks live on the Tasks tab — we
  // intentionally don't surface them here, so a promoted input keeps showing
  // its original envelope (subject/content/agent trace) rather than a
  // duplicate task card.
  const items = useMemo<InboxItem[]>(
    () =>
      inputs
        .map<InboxItem>((r) => ({
          id: r.id,
          sort: r.received_at,
          data: r,
        }))
        .sort((a, b) => new Date(b.sort).getTime() - new Date(a.sort).getTime()),
    [inputs],
  );

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
        <InboxCard
          key={it.id}
          item={it}
          onChanged={onChanged}
          seenAfter={seenAfter}
        />
      ))}
      {loadMoreButton}
    </div>
  );
}
