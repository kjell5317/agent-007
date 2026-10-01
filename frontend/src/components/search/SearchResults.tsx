import { useEffect, useRef, useState } from "react";
import { SearchResultRow } from "@/components/search/SearchResultRow";
import { Modal } from "@/components/ui/modal";
import { api } from "@/lib/api";
import type { Note, SearchHit, SearchHitType } from "@/lib/types";
import type { SearchFiltersState } from "@/components/search/SearchFilters";

function noteHit(note: Note): SearchHit {
  return {
    type: "note", id: note.id, title: note.content.slice(0, 80),
    snippet: note.content, url: null, task_id: null,
    source: note.source, sender: note.source_from, status: null,
    ts: note.updated_at, score: 0,
  };
}

function localTypes(filters: SearchFiltersState): SearchHitType[] | undefined {
  switch (filters.kind) {
    case "tasks": return ["task"];
    case "messages": return ["input"];
    case "events":
    case "files": return ["document"];
    case "contacts":
    case "notes": return [];
    default: return undefined;
  }
}

function filterHit(hit: SearchHit, filters: SearchFiltersState): boolean {
  if (filters.kind === "events") return hit.type === "document" && hit.source === "calendar";
  if (filters.kind === "files") {
    if (hit.type !== "drive" && (hit.type !== "document" || hit.source === "calendar")) return false;
    if (filters.format && hit.meta?.mime !== filters.format) return false;
  }
  if (filters.kind === "contacts") return hit.type === "contact";
  if (filters.kind === "notes") return hit.type === "note";
  return true;
}

export function SearchResults({
  query, filters, onOpenTask,
}: {
  query: string;
  filters: SearchFiltersState;
  onOpenTask: (id: string) => void;
}) {
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [preview, setPreview] = useState<SearchHit | null>(null);
  const notesCacheRef = useRef<Promise<Note[]> | null>(null);
  const needsQuery = (filters.kind === "contacts" && query.trim().length < 3) ||
    (filters.kind === "files" && Boolean(filters.format) && query.trim().length < 3);

  useEffect(() => {
    let cancelled = false;
    if (needsQuery) {
      setHits([]);
      setLoading(false);
      setFailed(false);
      return;
    }
    setLoading(true);
    setFailed(false);
    const timer = window.setTimeout(async () => {
      const q = query.trim();
      const types = localTypes(filters);
      const local = types?.length === 0
        ? null
        : api.suggest(q, 25, types, {
            source: filters.kind === "events" ? "calendar"
              : filters.kind === "messages" ? filters.source : undefined,
            label: filters.kind === "tasks" ? filters.label : undefined,
            excludeSource: filters.kind === "files" ? "calendar" : undefined,
          });
      const external = q.length >= 3 && (filters.kind === null || filters.kind === "files" || filters.kind === "contacts")
        ? api.suggestExternal(q, 10,
            filters.kind === "contacts" ? "contact" : filters.kind === "files" ? "drive" : undefined,
            filters.kind === "files" ? filters.format || undefined : undefined)
        : null;
      const notes = filters.kind === null || filters.kind === "notes"
        ? (notesCacheRef.current ??= api.listNotes(500).catch((error) => {
            notesCacheRef.current = null;
            throw error;
          }))
        : null;
      const results = await Promise.allSettled([local, external, notes]);
      if (cancelled) return;
      setFailed(!results.some((result) => result.status === "fulfilled" && result.value !== null));
      const localHits = results[0].status === "fulfilled" ? results[0].value?.hits ?? [] : [];
      const externalHits = results[1].status === "fulfilled" ? results[1].value?.hits ?? [] : [];
      const noteHits = results[2].status === "fulfilled"
        ? (results[2].value ?? []).filter((note) => !q || note.content.toLowerCase().includes(q.toLowerCase())).map(noteHit)
        : [];
      const all = [...localHits, ...externalHits, ...noteHits]
        .filter((hit) => filterHit(hit, filters));
      setHits(all.slice(0, 35));
      setLoading(false);
    }, 180);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [query, filters, needsQuery]);

  return (
    <div className="space-y-2 pt-2">
      {needsQuery ? (
        <p className="py-12 text-center text-sm text-muted-foreground">Enter at least 3 characters to search {filters.kind === "contacts" ? "contacts" : "Drive files"}.</p>
      ) : loading ? (
        <p className="py-12 text-center text-sm text-muted-foreground">Searching…</p>
      ) : failed ? (
        <p className="py-12 text-center text-sm text-muted-foreground">Search is unavailable right now.</p>
      ) : hits.length === 0 ? (
        <p className="py-12 text-center text-sm text-muted-foreground">No matching results.</p>
      ) : (
        <div className="space-y-2">
          <p className="px-1 text-xs font-medium text-muted-foreground">{hits.length} results</p>
          {hits.map((hit) => (
            <SearchResultRow
              key={`${hit.type}:${hit.id}`}
              hit={hit}
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
