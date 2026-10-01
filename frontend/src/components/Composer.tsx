import { useEffect, useRef, useState, type FormEvent } from "react";
import { Plus } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { pollTaskCreation, type PollHandle } from "@/lib/pollTask";

interface Props {
  onCreated: () => Promise<void> | void;
}

export function Composer({ onCreated }: Props) {
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const postingText = useRef<Set<string>>(new Set());
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
    <div className="fixed inset-x-0 bottom-[env(safe-area-inset-bottom)] z-40">
      <form onSubmit={submit} autoComplete="off">
        <div className="mx-auto max-w-2xl px-4 py-2">
          <div className="flex items-center gap-2">
            <div className="flex h-12 min-w-0 flex-1 items-center rounded-full bg-secondary px-4 shadow-sm">
              <Input
                ref={inputRef}
                value={value}
                onChange={(e) => setValue(e.target.value)}
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
                className="h-10 min-w-0 flex-1 border-0 bg-transparent px-0 text-sm shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
              />
            </div>
            <Button type="submit" disabled={!value.trim()} aria-label="Add task" title="Add task" className="h-12 w-12 shrink-0 rounded-full p-0 shadow-sm">
              <Plus className="h-5 w-5" />
            </Button>
          </div>
        </div>
      </form>
    </div>
  );
}
