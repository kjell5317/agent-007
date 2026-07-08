import { SkeletonBlock } from "@/components/ui/skeleton";
import { SearchResultRow } from "@/components/search/SearchResultRow";
import { useSearchStream } from "@/hooks/useSearchStream";

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
          <SkeletonBlock key={i} className="h-14 w-full rounded-xl" />
        ))}
      </div>
    );
  }

  if (hits.length === 0) {
    return (
      <div className="py-16 text-center text-sm text-muted-foreground">
        {trimmed
          ? `No results for “${trimmed}”.`
          : "Search your tasks, inbox and calendar."}
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
      <ul className="space-y-2">
        {hits.map((hit) => (
          <li key={`${hit.type}:${hit.id}`}>
            <SearchResultRow hit={hit} onOpenTask={onOpenTask} />
          </li>
        ))}
      </ul>
    </div>
  );
}
