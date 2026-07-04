import { useMemo, useState } from "react";
import { InboxCard } from "@/components/inbox/InboxCard";
import { InboxGroup } from "@/components/inbox/InboxGroup";
import { Button } from "@/components/ui/button";
import { groupInputs } from "@/lib/inbox";
import type { RawInput } from "@/lib/types";

interface Props {
  inputs: RawInput[];
  onChanged: () => Promise<void> | void;
  onLoadMore: () => Promise<void>;
  hasMore: boolean;
  unseenInputIds: ReadonlySet<string>;
  onInputsVisible: (ids: string[]) => void;
  onOpenTask: (id: string) => void;
}

export function InboxPanel({
  inputs,
  onChanged,
  onLoadMore,
  hasMore,
  unseenInputIds,
  onInputsVisible,
  onOpenTask,
}: Props) {
  const [loadingMore, setLoadingMore] = useState(false);

  // Inbox is a raw-input log/debug view. Tasks live on the Tasks tab — we
  // intentionally don't surface them here, so a promoted input keeps showing
  // its original envelope (subject/content/agent trace) rather than a
  // duplicate task card. Inputs that share a thread / task are folded into a
  // single group dropdown; everything else stays a standalone card.
  const groups = useMemo(() => groupInputs(inputs), [inputs]);

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

  if (groups.length === 0) {
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
      {groups.map((group) =>
        group.members.length === 1 ? (
          <InboxCard
            key={group.key}
            item={{
              id: group.newest.id,
              sort: group.sort,
              data: group.newest,
            }}
            onChanged={onChanged}
            unseen={unseenInputIds.has(group.newest.id)}
            onVisible={(id) => onInputsVisible([id])}
            onOpenTask={onOpenTask}
          />
        ) : (
          <InboxGroup
            key={group.key}
            group={group}
            onChanged={onChanged}
            unseenMemberIds={group.members
              .filter((member) => unseenInputIds.has(member.id))
              .map((member) => member.id)}
            onVisible={onInputsVisible}
            onOpenTask={onOpenTask}
          />
        ),
      )}
      {loadMoreButton}
    </div>
  );
}
