import { useEffect, useRef, useState, type FormEvent } from "react";
import { Plus } from "lucide-react";
import { toast } from "sonner";
import { SearchResultRow } from "@/components/search/SearchResultRow";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { useLiveSuggestions } from "@/hooks/useLiveSuggestions";
import { pollTaskCreation, type PollHandle } from "@/lib/pollTask";

interface Props {
  onCreated: () => Promise<void> | void;
  onOpenTask: (taskId: string) => void;
}

export function Composer({ onCreated, onOpenTask }: Props) {
  const [value, setValue] = useState("");
  const suggestions = useLiveSuggestions(value);
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

  useEffect(() => {
    setDismissed(false);
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
          toast.success("Task saved");
          void Promise.resolve(onCreated()).catch(() => {});
        }),
      onFailure: (message) => finish(() => {
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
    setValue("");
    if (inputRef.current) inputRef.current.value = "";
    setDismissed(true);
    inputRef.current?.focus();
    // Show the loading toast immediately — the POST itself takes a moment,
    // so without this the user gets no feedback until polling starts.
    const toastId = toast.loading("Saving task…", { duration: Infinity });
    try {
      const { raw_input_id } = await api.createTask(text);
      trackPoll(raw_input_id, toastId);
    } catch (err) {
      setValue((current) => current || text);
      toast.dismiss(toastId);
      toast.error((err as Error).message);
    } finally {
      postingText.current.delete(text);
    }
  };

  return (
    <div className="fixed inset-x-0 bottom-[calc(4.125rem+env(safe-area-inset-bottom))] z-40">
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
      <form onSubmit={submit} autoComplete="off">
        <div className="mx-auto max-w-2xl px-4 py-2">
          <div className="flex h-12 items-center gap-3 rounded-full bg-secondary px-4 shadow-sm">
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
              className="h-10 min-w-0 flex-1 border-0 bg-transparent px-0 text-sm shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
            />
            <Button type="submit" disabled={!value.trim()} aria-label="Add task" title="Add task" className="h-8 w-8 shrink-0 rounded-full p-0">
              <Plus className="h-5 w-5" />
            </Button>
          </div>
        </div>
      </form>
    </div>
  );
}
