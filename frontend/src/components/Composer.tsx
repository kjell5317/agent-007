import { useEffect, useRef, useState, type FormEvent } from "react";
import { CalendarDays, FileText, Inbox, ListTodo, StickyNote } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { pollTaskCreation, type PollHandle } from "@/lib/pollTask";
import type { SearchHit, SearchHitType } from "@/lib/types";
import { cn } from "@/lib/utils";

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

const TYPE_ICON: Record<SearchHitType, typeof Inbox> = {
  task: ListTodo,
  input: Inbox,
  note: StickyNote,
  document: FileText,
};

function hitIcon(hit: SearchHit) {
  if (hit.type === "document" && hit.source === "calendar") return CalendarDays;
  return TYPE_ICON[hit.type];
}

export function Composer({ onCreated, onOpenTask }: Props) {
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [suggestions, setSuggestions] = useState<SearchHit[]>([]);
  const [dismissed, setDismissed] = useState(false);
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

  const openHit = (hit: SearchHit) => {
    setDismissed(true);
    if (hit.task_id) onOpenTask(hit.task_id);
    else if (hit.url) window.open(hit.url, "_blank", "noopener,noreferrer");
  };

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
          <ul
            className="mb-2 max-h-[45dvh] overflow-y-auto overscroll-contain rounded-xl border bg-card shadow-lg"
            role="listbox"
          >
            {suggestions.map((hit) => {
              const Icon = hitIcon(hit);
              return (
                <li key={`${hit.type}:${hit.id}`}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={false}
                    // Fire before the input's blur so the click isn't lost.
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => openHit(hit)}
                    className="flex w-full items-center gap-3 border-b px-3 py-2.5 text-left last:border-b-0 hover:bg-accent hover:text-accent-foreground"
                  >
                    <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">
                        {hit.title || "Untitled"}
                      </span>
                      {hit.snippet && (
                        <span className="block truncate text-xs text-muted-foreground">
                          {hit.snippet}
                        </span>
                      )}
                    </span>
                    <span className="shrink-0 rounded-full bg-secondary px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                      {hit.source ?? hit.type}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
      <form
        onSubmit={submit}
        autoComplete="off"
        className={cn(
          "border-t bg-card pb-[env(safe-area-inset-bottom)]",
          "shadow-[0_-4px_14px_rgba(15,23,42,0.06)] dark:shadow-[0_-4px_18px_rgba(0,0,0,0.35)]",
        )}
      >
        <div className="mx-auto flex max-w-2xl items-center gap-2 px-3 py-2.5">
          <Input
            value={value}
            onChange={(e) => setValue(e.target.value)}
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
