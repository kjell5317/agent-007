import { useEffect, useRef, useState } from "react";
import { SearchResultRow } from "@/components/search/SearchResultRow";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import type { SearchHit, SearchHitType } from "@/lib/types";

const SUGGEST_DEBOUNCE_MS = 150;
// Mirror the task composer: suggest existing tasks (→ modal) and documents
// (calendar events → calendar, kotx briefs → their task). Inputs and notes are
// out. The server restricts to these via `types` so the limit isn't spent on
// other corpora.
const SUGGESTIBLE: ReadonlySet<SearchHitType> = new Set(["task", "document"]);
const SUGGEST_TYPES: readonly SearchHitType[] = ["task", "document"];

export function ChatComposer({
  onSend,
  streaming,
  onClose,
  onOpenTask,
}: {
  onSend: (text: string) => void;
  streaming: boolean;
  onClose: () => void;
  onOpenTask: (taskId: string) => void;
}) {
  const [value, setValue] = useState("");
  const [suggestions, setSuggestions] = useState<SearchHit[]>([]);
  const [dismissed, setDismissed] = useState(false);
  const listRef = useRef<HTMLUListElement>(null);

  // Suggest-as-you-type (same source as the task composer). Debounced, latest-wins.
  useEffect(() => {
    const q = value.trim();
    if (q.length < 1) {
      setSuggestions([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const { hits } = await api.suggest(q, 8, SUGGEST_TYPES);
        if (cancelled) return;
        setSuggestions(hits.filter((h) => SUGGESTIBLE.has(h.type)).slice(0, 6));
        setDismissed(false);
      } catch {
        if (!cancelled) setSuggestions([]);
      }
    }, SUGGEST_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [value]);

  const showSuggestions = !dismissed && value.trim().length >= 1 && suggestions.length > 0;

  // Keep the best (last) suggestion in view when the list overflows.
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [suggestions, showSuggestions]);

  const submit = () => {
    const text = value.trim();
    if (!text || streaming) return;
    onSend(text);
    setValue("");
    setSuggestions([]);
    setDismissed(true);
  };

  return (
    <div className="fixed inset-x-0 bottom-0 z-40">
      {showSuggestions && (
        <div className="mx-auto max-w-2xl px-3">
          <ul
            ref={listRef}
            className="mb-2 max-h-[calc(100dvh-5rem)] space-y-1.5 overflow-y-auto overscroll-contain rounded-2xl border-2 border-border bg-background p-2 shadow-2xl sm:max-h-[45dvh]"
            role="listbox"
          >
            {[...suggestions].reverse().map((hit) => (
              <li key={`${hit.type}:${hit.id}`} role="option" aria-selected={false}>
                <SearchResultRow
                  hit={hit}
                  onOpenTask={onOpenTask}
                  onActivate={() => setDismissed(true)}
                  preventBlur
                />
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="border-t bg-card pb-[env(safe-area-inset-bottom)] shadow-[0_-4px_14px_rgba(15,23,42,0.06)] dark:shadow-[0_-4px_18px_rgba(0,0,0,0.35)]">
        <div className="mx-auto flex max-w-2xl items-center gap-2 px-3 py-2.5">
          <Input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onFocus={() => {
              if (value.trim().length >= 1) setDismissed(false);
            }}
            onBlur={() => window.setTimeout(() => setDismissed(true), 100)}
            placeholder="Ask anything…"
            enterKeyHint="send"
            autoCapitalize="sentences"
            autoCorrect="off"
            autoComplete="off"
            autoFocus
            role="combobox"
            aria-expanded={showSuggestions}
            aria-autocomplete="list"
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              } else if (e.key === "Escape") {
                if (showSuggestions) setDismissed(true);
                else onClose();
              }
            }}
            className="h-10 rounded-full bg-secondary px-4 text-[15px]"
          />
          <Button onClick={submit} disabled={streaming || !value.trim()} className="px-5">
            Ask
          </Button>
        </div>
      </div>
    </div>
  );
}
