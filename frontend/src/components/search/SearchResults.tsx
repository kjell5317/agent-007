import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { SearchResultRow } from "@/components/search/SearchResultRow";
import { Modal } from "@/components/ui/modal";
import { api } from "@/lib/api";
import { searchDistance } from "@/lib/searchRanking";
import type { Note, SearchHit, Task } from "@/lib/types";
import type { SearchFiltersState } from "@/components/search/SearchFilters";

function noteHit(note: Note): SearchHit {
  return {
    type: "note", id: note.id, title: note.content.slice(0, 80),
    snippet: note.content, url: null, task_id: null,
    source: note.source, sender: note.source_from, status: null,
    ts: note.updated_at, score: 0,
  };
}

const PER_SOURCE = 10;

function filterHit(hit: SearchHit, filters: SearchFiltersState): boolean {
  if (filters.kind === "events") return hit.type === "document" && hit.source === "calendar";
  if (filters.kind === "files") {
    if (hit.type !== "drive" && (hit.type !== "document" || hit.source === "calendar")) return false;
    if (filters.format && hit.meta?.mime !== filters.format) return false;
  }
  if (filters.kind === "contacts") return hit.type === "contact";
  if (filters.kind === "notes") return hit.type === "note" && (!filters.source || (hit.source ?? "chat") === filters.source);
  return true;
}

function searchRequests(query: string, filters: SearchFiltersState, submitted: boolean): Promise<{ hits: SearchHit[] }>[] {
  const requests: Promise<{ hits: SearchHit[] }>[] = [];
  if (filters.kind === null || filters.kind === "tasks") {
    requests.push(api.suggest(query, PER_SOURCE, ["task"], { label: filters.kind === "tasks" ? filters.label : undefined }));
  }
  if (filters.kind === null || filters.kind === "messages") {
    requests.push(api.suggest(query, PER_SOURCE, ["input"], { source: filters.kind === "messages" ? filters.source : undefined }));
  }
  if (filters.kind === null || filters.kind === "events") {
    requests.push(api.suggest(query, PER_SOURCE, ["document"], { source: "calendar" }));
  }
  if (filters.kind === null || filters.kind === "files") {
    requests.push(api.suggest(query, PER_SOURCE, ["document"], { excludeSource: "calendar" }));
  }
  if (filters.kind === null || filters.kind === "notes") {
    requests.push(api.listNotes(PER_SOURCE, filters.kind === "notes" ? filters.source : "", query)
      .then((notes) => ({ hits: notes.map(noteHit) })));
  }
  if (submitted) {
    if (filters.kind === null || filters.kind === "files") {
      requests.push(api.suggestExternal(query, PER_SOURCE, "drive", filters.kind === "files" ? filters.format || undefined : undefined));
    }
    if (filters.kind === null || filters.kind === "contacts") {
      requests.push(api.suggestExternal(query, PER_SOURCE, "contact"));
    }
    if (filters.kind === null) {
      requests.push(api.suggestExternal(query, PER_SOURCE, "github"));
    }
  }
  return requests;
}

export function SearchResults({
  query, filters, tasks, submitted, onNoResults, onOpenTask,
}: {
  query: string;
  filters: SearchFiltersState;
  tasks: Task[];
  submitted: boolean;
  onNoResults: (query: string) => void;
  onOpenTask: (id: string) => void;
}) {
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [preview, setPreview] = useState<SearchHit | null>(null);
  const onNoResultsRef = useRef(onNoResults);
  onNoResultsRef.current = onNoResults;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFailed(false);
    const timer = window.setTimeout(async () => {
      const q = query.trim();
      const requests = searchRequests(q, filters, submitted);
      const results = await Promise.allSettled(requests);
      if (cancelled) return;
      const all = results.flatMap((result) => result.status === "fulfilled" ? result.value.hits : [])
        .filter((hit) => filterHit(hit, filters));
      const taskIds = new Set(all.filter((hit) => hit.type === "task").map((hit) => hit.id));
      const seen = new Set<string>();
      const unique = all.filter((hit) => {
        if (hit.type === "document" && hit.task_id && taskIds.has(hit.task_id)) return false;
        const key = `${hit.type}:${hit.id}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
      const taskMap = new Map(tasks.map((task) => [task.id, task]));
      const now = Date.now();
      unique.sort((a, b) => searchDistance(a, taskMap, now) - searchDistance(b, taskMap, now) ||
        b.score - a.score || a.title.localeCompare(b.title));
      const hadFailure = results.some((result) => result.status === "rejected");
      if (submitted && unique.length === 0 && !hadFailure) {
        onNoResultsRef.current(q);
        return;
      }
      setFailed(unique.length === 0 && hadFailure);
      setHits(unique);
      setLoading(false);
    }, submitted ? 0 : 180);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [query, filters, submitted, tasks]);

  return (
    <div className="space-y-2">
      {loading ? (
        <div className="flex justify-center py-12" role="status" aria-label="Searching"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
      ) : failed ? (
        <p className="py-12 text-center text-sm text-muted-foreground">Search is unavailable right now.</p>
      ) : hits.length === 0 ? (
        submitted ? <p className="py-12 text-center text-sm text-muted-foreground">No matching results.</p> : null
      ) : (
        <div className="space-y-2">
          <p className="px-1 text-xs font-medium text-muted-foreground">{hits.length} results</p>
          {hits.map((hit) => (
            <SearchResultRow
              key={`${hit.type}:${hit.id}`}
              hit={hit}
              task={hit.type === "task" ? tasks.find((task) => task.id === hit.id) : undefined}
              onOpenTask={onOpenTask}
              onShowContent={() => setPreview(hit)}
            />
          ))}
        </div>
      )}
      <Modal open={preview !== null} onClose={() => setPreview(null)} title={preview?.title || "Result"}>
        <p className="max-h-[60dvh] overflow-y-auto whitespace-pre-wrap text-sm">{preview?.snippet || "No preview available."}</p>
      </Modal>
    </div>
  );
}
