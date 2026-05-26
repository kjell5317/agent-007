import { useEffect, useRef, useState, type FormEvent } from "react";
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
  const [submitting, setSubmitting] = useState(false);
  // Stop in-flight polls when the component unmounts.
  const activePolls = useRef<Set<PollHandle>>(new Set());

  useEffect(
    () => () => {
      activePolls.current.forEach((handle) => handle.cancel());
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
    // Show the loading toast immediately — the POST itself takes a moment,
    // so without this the user gets no feedback until polling starts.
    const toastId = toast.loading("Creating task…", { duration: Infinity });
    try {
      const { raw_input_id } = await api.createTask(text);
      setValue("");
      trackPoll(raw_input_id, toastId);
    } catch (err) {
      toast.dismiss(toastId);
      toast.error((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={submit}
      className="fixed inset-x-0 bottom-0 z-40 border-t bg-card pb-[env(safe-area-inset-bottom)] shadow-[0_-4px_14px_rgba(15,23,42,0.06)]"
    >
      <div className="mx-auto flex max-w-2xl items-center gap-2 px-3 py-2.5">
        <Input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Add a task…"
          enterKeyHint="send"
          autoCapitalize="sentences"
          autoComplete="off"
          className="h-10 rounded-full bg-secondary px-4 text-[15px]"
        />
        <Button type="submit" disabled={submitting || !value.trim()} className="rounded-full px-5">
          Add
        </Button>
      </div>
    </form>
  );
}
