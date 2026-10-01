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
  onPending: (text: string) => string;
  onAccepted: (id: string, rawInputId: string) => void;
  onSettled: (id: string) => void;
}

const SUGGEST_DEBOUNCE_MS = 150;
// The composer helps you jump to something that already exists instead of
// creating a duplicate: existing tasks (→ modal) and documents (calendar events
// → calendar, kotx briefs → their task). Inputs and notes are out — you're
// adding a task, so only task-shaped destinations are useful here. The server
// restricts to these via `types` so the limit isn't spent on other corpora.
const SUGGESTIBLE: ReadonlySet<SearchHitType> = new Set(["task", "document"]);
const SUGGEST_TYPES: readonly SearchHitType[] = ["task", "document"];

export function Composer({ onCreated, onOpenTask, onPending, onAccepted, onSettled }: Props) {
  const [value, setValue] = useState("");
  const [suggestions, setSuggestions] = useState<SearchHit[]>([]);
  const [dismissed, setDismissed] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const postingText = useRef<Set<string>>(new Set());
  const listRef = useRef<HTMLUListElement>(null);
  // Stop in-flight polls when the component unmounts.
  const activePolls = useRef<Map<PollHandle, string | number>>(new Map());

  useEffect(
    () => () => {
      activePolls.current.forEach((toastId, handle) => {
        handle.cancel();
        toast.dismiss(toastId);
      });
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

  // Best result sits at the bottom (nearest the input); keep it in view when
  // the list overflows and has to scroll.
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [suggestions, showSuggestions]);

  const trackPoll = (rawInputId: string, toastId: string | number, pendingId: string) => {
    let handle: PollHandle | null = null;
    const finish = (run: () => void) => {
      toast.dismiss(toastId);
      run();
      if (handle) activePolls.current.delete(handle);
    };
    handle = pollTaskCreation(rawInputId, {
      onSuccess: () =>
        finish(() => {
          toast.success("Task saved");
          void Promise.resolve(onCreated()).catch(() => {}).finally(() => onSettled(pendingId));
        }),
      onFailure: (message) => finish(() => {
        onSettled(pendingId);
        toast.error(message);
      }),
      onTimeout: () =>
        finish(() => toast.error("Task is taking longer than expected")),
    });
    activePolls.current.set(handle, toastId);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const text = inputRef.current?.value.trim() ?? value.trim();
    if (!text || postingText.current.has(text)) return;
    postingText.current.add(text);
    const pendingId = onPending(text);
    setValue("");
    if (inputRef.current) inputRef.current.value = "";
    setSuggestions([]);
    setDismissed(true);
    inputRef.current?.focus();
    // Show the loading toast immediately — the POST itself takes a moment,
    // so without this the user gets no feedback until polling starts.
    const toastId = toast.loading("Saving task…", { duration: Infinity });
    try {
      const { raw_input_id } = await api.createTask(text);
      onAccepted(pendingId, raw_input_id);
      trackPoll(raw_input_id, toastId, pendingId);
    } catch (err) {
      onSettled(pendingId);
      setValue((current) => current || text);
      toast.dismiss(toastId);
      toast.error((err as Error).message);
    } finally {
      postingText.current.delete(text);
    }
  };

  return (
    <div className="fixed inset-x-0 bottom-0 z-40">
      {showSuggestions && (
        <div className="mx-auto max-w-2xl px-3">
          {/* Elevated panel so the suggestions read as a distinct surface
              floating above the task list; solid (opaque) background + border,
              cards inside separated by gaps. */}
          <ul
            ref={listRef}
            className="mb-2 max-h-[calc(100dvh-5rem)] space-y-1.5 overflow-y-auto overscroll-contain rounded-2xl border-2 border-border bg-background p-2 shadow-2xl sm:max-h-[45dvh]"
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
            ref={inputRef}
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
          <Button type="submit" disabled={!value.trim()} className="px-5">
            Add
          </Button>
        </div>
      </form>
    </div>
  );
}
