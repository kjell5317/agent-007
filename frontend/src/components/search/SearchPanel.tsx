import { CalendarDays, FileText, Inbox, ListTodo, StickyNote } from "lucide-react";
import type { ComponentType } from "react";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { useSearchStream } from "@/hooks/useSearchStream";
import { cn } from "@/lib/utils";
import type { SearchHit, SearchHitType } from "@/lib/types";

const TYPE_ICON: Record<SearchHitType, ComponentType<{ className?: string }>> = {
  task: ListTodo,
  input: Inbox,
  note: StickyNote,
  document: FileText,
};

function hitIcon(hit: SearchHit): ComponentType<{ className?: string }> {
  if (hit.type === "document" && hit.source === "calendar") return CalendarDays;
  return TYPE_ICON[hit.type];
}

// Short label shown on the right of a row: the origin when we have one
// (gmail, calendar, …), otherwise the corpus name.
function hitBadge(hit: SearchHit): string {
  return hit.source ?? hit.type;
}

export function SearchPanel({
  query,
  onOpenTask,
}: {
  query: string;
  onOpenTask: (taskId: string) => void;
}) {
  const { hits, loading } = useSearchStream(query);
  const trimmed = query.trim();

  if (loading && hits.length === 0) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <SkeletonBlock key={i} className="h-14 w-full" />
        ))}
      </div>
    );
  }

  if (hits.length === 0) {
    return (
      <div className="py-16 text-center text-sm text-muted-foreground">
        {trimmed ? `No results for “${trimmed}”.` : "Search your tasks, inbox, notes and calendar."}
      </div>
    );
  }

  return (
    <div>
      {!trimmed && (
        <div className="mb-2 px-1 text-xs font-medium uppercase tracking-wider text-muted-foreground">
          Recent
        </div>
      )}
      <ul className="space-y-1.5">
        {hits.map((hit) => (
          <SearchResultRow key={`${hit.type}:${hit.id}`} hit={hit} onOpenTask={onOpenTask} />
        ))}
      </ul>
    </div>
  );
}

function SearchResultRow({
  hit,
  onOpenTask,
}: {
  hit: SearchHit;
  onOpenTask: (taskId: string) => void;
}) {
  const Icon = hitIcon(hit);
  const openTask = hit.task_id;
  const openUrl = !openTask && hit.url ? hit.url : null;
  const clickable = Boolean(openTask || openUrl);

  const activate = () => {
    if (openTask) onOpenTask(openTask);
    else if (openUrl) window.open(openUrl, "_blank", "noopener,noreferrer");
  };

  return (
    <li>
      <div
        role={clickable ? "button" : undefined}
        tabIndex={clickable ? 0 : undefined}
        onClick={clickable ? activate : undefined}
        onKeyDown={
          clickable
            ? (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  activate();
                }
              }
            : undefined
        }
        className={cn(
          "flex items-center gap-3 rounded-lg border bg-card px-3 py-2.5 text-left",
          clickable && "cursor-pointer hover:bg-accent hover:text-accent-foreground",
        )}
      >
        <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{hit.title || "Untitled"}</div>
          {hit.snippet && (
            <div className="truncate text-xs text-muted-foreground">{hit.snippet}</div>
          )}
        </div>
        <span className="shrink-0 rounded-full bg-secondary px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
          {hitBadge(hit)}
        </span>
      </div>
    </li>
  );
}
