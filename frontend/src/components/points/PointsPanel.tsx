import { Minus, Plus } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { MetaDot } from "@/components/inbox/InboxCard";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api, type PointsLogEntry } from "@/lib/api";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 20;
const QUICK_DELTA = 10;

interface Props {
  onOpenTask: (id: string) => void;
}

export function PointsPanel({ onOpenTask }: Props) {
  const [entries, setEntries] = useState<PointsLogEntry[]>([]);
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [hasMore, setHasMore] = useState(false);
  const [seenBefore, setSeenBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [adjusting, setAdjusting] = useState<number | null>(null);
  const seenBeforeRef = useRef<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const loadLog = useCallback(async (nextLimit: number, initial = false) => {
    if (initial) setLoading(true);
    const res = await api.getPointsLog(nextLimit);
    if (!mountedRef.current) return;
    setEntries(res.entries);
    setHasMore(res.has_more);
    if (seenBeforeRef.current == null) {
      seenBeforeRef.current = res.last_seen_at;
      setSeenBefore(res.last_seen_at);
    }
    if (initial) {
      setLoading(false);
      void api.markPointsLogSeen();
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loadLog(PAGE_SIZE, true).catch((err) => {
      if (cancelled) return;
      setLoading(false);
      toast.error(`Failed to load points: ${(err as Error).message}`);
    });
    return () => {
      cancelled = true;
    };
  }, [loadLog]);

  const adjust = async (delta: number) => {
    setAdjusting(delta);
    try {
      await api.adjustPoints(delta, { caller: "Manual" });
      toast.success(`${formatSignedPoints(delta)} points`);
      await loadLog(limit);
    } catch (err) {
      toast.error(`Failed: ${(err as Error).message}`);
    } finally {
      setAdjusting(null);
    }
  };

  const loadMore = async () => {
    const next = limit + PAGE_SIZE;
    setLoadingMore(true);
    try {
      await loadLog(next);
      setLimit(next);
    } catch (err) {
      toast.error(`Failed to load more: ${(err as Error).message}`);
    } finally {
      setLoadingMore(false);
    }
  };

  const seenTime = seenBefore == null ? null : new Date(seenBefore).getTime();

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <Button
          variant="outline"
          className="h-14 justify-center gap-2"
          disabled={adjusting != null}
          onClick={() => adjust(-QUICK_DELTA)}
        >
          <Minus className="h-5 w-5" />
          <span className="tabular-nums">10</span>
        </Button>
        <Button
          className="h-14 justify-center gap-2"
          disabled={adjusting != null}
          onClick={() => adjust(QUICK_DELTA)}
        >
          <Plus className="h-5 w-5" />
          <span className="tabular-nums">10</span>
        </Button>
      </div>

      {loading ? (
        <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
          Loading...
        </div>
      ) : entries.length === 0 ? (
        <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
          No point changes yet.
        </div>
      ) : (
        <div className="space-y-2">
          {entries.map((entry) => (
            <PointsCard
              key={entry.id}
              entry={entry}
              unseen={
                seenTime != null &&
                new Date(entry.created_at).getTime() > seenTime
              }
              onOpenTask={onOpenTask}
            />
          ))}
        </div>
      )}

      {hasMore && (
        <div className="flex justify-center pt-2">
          <Button
            variant="outline"
            size="sm"
            onClick={loadMore}
            disabled={loadingMore}
          >
            {loadingMore ? "Loading..." : "Load more"}
          </Button>
        </div>
      )}
    </div>
  );
}

function PointsCard({
  entry,
  unseen,
  onOpenTask,
}: {
  entry: PointsLogEntry;
  unseen: boolean;
  onOpenTask: (id: string) => void;
}) {
  const clickable = entry.task_id != null;

  return (
    <Card
      className={cn(
        unseen && "border-emerald-500/70",
        clickable && "transition-colors hover:border-primary/50",
      )}
    >
      <CardContent
        className={cn(clickable && "cursor-pointer")}
        onClick={() => {
          if (entry.task_id) onOpenTask(entry.task_id);
        }}
      >
        <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              {unseen && (
                <span
                  aria-label="Unseen"
                  title="Unseen"
                  className="inline-block h-2 w-2 shrink-0 rounded-full bg-emerald-500"
                />
              )}
              <div className="min-w-0 flex-1 truncate font-medium leading-snug">
                {entry.reason}
              </div>
            </div>
            <div className="mt-1 flex min-w-0 items-center gap-2 overflow-hidden text-xs text-muted-foreground">
              <span className="shrink-0 font-medium">
                {formatLogTime(entry.created_at)}
              </span>
              {entry.caller && (
                <>
                  <MetaDot />
                  <span className="min-w-0 flex-1 truncate font-medium">
                    {entry.caller}
                  </span>
                </>
              )}
            </div>
          </div>
          <div
            className={cn(
              "text-sm font-semibold tabular-nums",
              entry.amount >= 0 ? "text-emerald-500" : "text-destructive",
            )}
          >
            {formatSignedPoints(entry.amount)}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function formatSignedPoints(n: number): string {
  if (n === 0) return "0";
  return `${n > 0 ? "+" : "-"}${Math.round(Math.abs(n))}`;
}

function formatLogTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}
