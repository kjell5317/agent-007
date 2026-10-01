import { useEffect, useRef, useState } from "react";
import { useLabels } from "@/hooks/useLabels";
import { api } from "@/lib/api";
import { labelChipOutlineStyle, labelChipStyle } from "@/lib/labels";
import { cn } from "@/lib/utils";

export type SearchKind = "tasks" | "messages" | "events" | "files" | "contacts" | "notes";

export interface SearchFiltersState {
  kind: SearchKind | null;
  label: string;
  source: string;
  format: string;
}

export const EMPTY_SEARCH_FILTERS: SearchFiltersState = {
  kind: null,
  label: "",
  source: "",
  format: "",
};

const KINDS: { key: SearchKind; label: string }[] = [
  { key: "tasks", label: "Tasks" },
  { key: "messages", label: "Messages" },
  { key: "notes", label: "Notes" },
  { key: "events", label: "Events" },
  { key: "files", label: "Files" },
  { key: "contacts", label: "Contacts" },
];

function selectedFirst<T>(items: T[], selected: T | undefined): T[] {
  return selected === undefined ? items : [selected, ...items.filter((item) => item !== selected)];
}

function sourceLabel(source: string): string {
  return source.split("_").map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join(" ");
}

const pillBase = "inline-flex h-7 max-w-40 shrink-0 items-center rounded-full border px-3 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const childPill = (selected: boolean) => cn(
  pillBase,
  selected
    ? "border-slate-600 bg-slate-600 text-white dark:border-slate-400 dark:bg-slate-400 dark:text-slate-950"
    : "border-slate-400/50 bg-slate-500/10 text-slate-700 hover:bg-slate-500/20 dark:text-slate-300",
);

export function SearchFilters({
  filters,
  query,
  onChange,
}: {
  filters: SearchFiltersState;
  query: string;
  onChange: (next: SearchFiltersState) => void;
}) {
  const labels = useLabels();
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [facets, setFacets] = useState<{ messages: string[]; files: string[]; notes: string[] }>({ messages: [], files: [], notes: [] });
  useEffect(() => {
    const kind = filters.kind;
    if (kind !== "messages" && kind !== "files" && kind !== "notes") return;
    let active = true;
    api.searchFacets(kind).then(({ options }) => {
      if (active) setFacets((current) => ({ ...current, [kind]: options }));
    }).catch(() => {
      if (active) setFacets((current) => ({ ...current, [kind]: [] }));
    });
    return () => { active = false; };
  }, [filters.kind]);
  const visibleKinds = query.trim() ? KINDS : KINDS.filter((kind) =>
    kind.key === "tasks" || kind.key === "messages" || kind.key === "notes",
  );
  const orderedKinds = filters.kind
    ? [KINDS.find((kind) => kind.key === filters.kind)!, ...visibleKinds.filter((kind) => kind.key !== filters.kind)]
    : visibleKinds;

  useEffect(() => {
    scrollerRef.current?.scrollTo({ left: 0, behavior: "smooth" });
  }, [filters.kind, filters.label, filters.source, filters.format]);

  const selectKind = (kind: SearchKind) => {
    onChange({ ...EMPTY_SEARCH_FILTERS, kind: filters.kind === kind ? null : kind });
  };

  return (
    <div ref={scrollerRef} aria-label="Search filters" className="-mx-4 flex items-center gap-2 overflow-x-auto px-4 pb-2 [overflow-anchor:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      {orderedKinds.slice(0, filters.kind ? 1 : undefined).map((kind) => (
        <button
          key={kind.key}
          type="button"
          aria-pressed={filters.kind === kind.key}
          onClick={() => selectKind(kind.key)}
          className={cn(
            pillBase,
            filters.kind === kind.key
              ? "border-primary bg-primary text-primary-foreground"
              : "border-primary/30 bg-primary/5 text-primary hover:bg-primary/15",
          )}
        >
          {kind.label}
        </button>
      ))}
      {filters.kind === "tasks" && (
        selectedFirst(labels, labels.find((label) => label.name === filters.label)).map((label) => {
          const selected = filters.label === label.name;
          return (
            <button
              key={label.name}
              type="button"
              aria-pressed={selected}
              title={label.description || label.name}
              onClick={() => onChange({ ...filters, label: selected ? "" : label.name })}
              className={cn(pillBase, selected ? "border-transparent bg-slate-600 text-white" : "bg-card")}
              style={selected ? labelChipStyle(label.color) : labelChipOutlineStyle(label.color)}
            >
              <span className="truncate">{label.name}</span>
            </button>
          );
        })
      )}
      {filters.kind === "messages" && selectedFirst(facets.messages, facets.messages.find((source) => source === filters.source)).map((source) => {
        const selected = filters.source === source;
        return (
          <button
            key={source}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange({ ...filters, source: selected ? "" : source })}
            className={childPill(selected)}
          >
            {sourceLabel(source)}
          </button>
        );
      })}
      {filters.kind === "notes" && selectedFirst(facets.notes, facets.notes.find((source) => source === filters.source)).map((source) => {
        const selected = filters.source === source;
        return (
          <button
            key={source}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange({ ...filters, source: selected ? "" : source })}
            className={childPill(selected)}
          >
            {sourceLabel(source)}
          </button>
        );
      })}
      {filters.kind === "files" && selectedFirst(facets.files, facets.files.find((format) => format === filters.format)).map((format) => {
        const selected = filters.format === format;
        return (
          <button
            key={format}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange({ ...filters, format: selected ? "" : format })}
            className={childPill(selected)}
          >
            {format}
          </button>
        );
      })}
    </div>
  );
}
