import { useEffect, useRef, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { SearchResultRow } from "@/components/search/SearchResultRow";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { pollTaskCreation, type PollHandle } from "@/lib/pollTask";
import type { SearchHit, SearchHitType } from "@/lib/types";

interface Props {
  onCreated: () => Promise<void> | void;
  onOpenTask: (taskId: string) => void;
}

const SUGGEST_DEBOUNCE_MS = 150;
// The composer helps you jump to something that already exists instead of
// creating a duplicate: existing tasks (→ modal), calendar events (→ calendar)
// and source inputs (→ their source). Notes aren't navigable and kotx docs are
// excluded server-side (their task already shows), so keep to these three.
const SUGGESTIBLE: ReadonlySet<SearchHitType> = new Set(["task", "input", "document"]);

export function Composer({ onCreated, onOpenTask }: Props) {
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [suggestions, setSuggestions] = useState<SearchHit[]>([]);
  const [dismissed, setDismissed] = useState(false);
  const listRef = useRef<HTMLUListElement>(null);
  // Stop in-flight polls when the component unmounts.
  const activePolls = useRef<Set<PollHandle>>(new Set());

  useEffect(
    () => () => {
      activePolls.current.forEach((handle) => handle.cancel());
      activePolls.current.clear();
    },
    [],
  );

  // Suggest-as-you-type, after the first character. Debounced, latest-wins.
  useEffect(() => {
    const q = value.trim();
    if (q.length < 1) {
      setSuggestions([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const { hits } = await api.suggest(q, 8);
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

  // Best result sits at the bottom (nearest the input); keep it in view when
  // the list overflows and has to scroll.
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [suggestions, showSuggestions]);

  const trackPoll = (rawInputId: string, toastId: string | number) => {
    let handle: PollHandle | null = null;
    const finish = (run: () => void) => {
      toast.dismiss(toastId);
      run();
      if (handle) activePolls.current.delete(handle);
    };
    handle = pollTaskCreation(rawInputId, {
      onSuccess: () =>
        finish(() => {
          toast.success("Task added");
          void onCreated();
        }),
      onFailure: (message) => finish(() => toast.error(message)),
      onTimeout: () =>
        finish(() => toast.error("Task is taking longer than expected")),
    });
    activePolls.current.add(handle);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const text = value.trim();
    if (!text || submitting) return;
    setSubmitting(true);
    setDismissed(true);
    // Show the loading toast immediately — the POST itself takes a moment,
    // so without this the user gets no feedback until polling starts.
    const toastId = toast.loading("Creating task…", { duration: Infinity });
    try {
      const { raw_input_id } = await api.createTask(text);
      setValue("");
      setSuggestions([]);
      trackPoll(raw_input_id, toastId);
    } catch (err) {
      toast.dismiss(toastId);
      toast.error((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-x-0 bottom-0 z-40">
      {showSuggestions && (
        <div className="mx-auto max-w-2xl px-3">
          {/* Elevated panel so the suggestions read as a distinct surface
              floating above the task list; cards inside are separated by gaps. */}
          <ul
            ref={listRef}
            className="mb-2 max-h-[45dvh] space-y-1.5 overflow-y-auto overscroll-contain rounded-2xl border bg-background/95 p-2 shadow-2xl backdrop-blur"
            role="listbox"
          >
            {/* Reversed: best match is rendered last so it sits at the bottom,
                closest to the input. */}
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
      <form
        onSubmit={submit}
        autoComplete="off"
        className="border-t bg-card pb-[env(safe-area-inset-bottom)] shadow-[0_-4px_14px_rgba(15,23,42,0.06)] dark:shadow-[0_-4px_18px_rgba(0,0,0,0.35)]"
      >
        <div className="mx-auto flex max-w-2xl items-center gap-2 px-3 py-2.5">
          <Input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onFocus={() => {
              // Re-show suggestions when returning to a field that still has text.
              if (value.trim().length >= 1) setDismissed(false);
            }}
            onBlur={() => window.setTimeout(() => setDismissed(true), 100)}
            placeholder="Add a task…"
            enterKeyHint="send"
            autoCapitalize="sentences"
            autoComplete="off"
            autoCorrect="off"
            // Suppress browser + password-manager autofill overlays on this field.
            name="task-title"
            data-1p-ignore
            data-lpignore="true"
            data-form-type="other"
            role="combobox"
            aria-expanded={showSuggestions}
            aria-autocomplete="list"
            className="h-10 rounded-full bg-secondary px-4 text-[15px]"
          />
          <Button type="submit" disabled={submitting || !value.trim()} className="px-5">
            Add
          </Button>
        </div>
      </form>
    </div>
  );
}
