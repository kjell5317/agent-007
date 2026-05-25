import { useEffect, useRef, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";

interface Props {
  onCreated: () => Promise<void> | void;
}

const POLL_INTERVAL_MS = 1000;
const POLL_TIMEOUT_MS = 120_000;

export function Composer({ onCreated }: Props) {
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // Stop in-flight polls when the component unmounts.
  const activePolls = useRef<Set<number>>(new Set());

  useEffect(
    () => () => {
      activePolls.current.forEach((id) => clearTimeout(id));
      activePolls.current.clear();
    },
    [],
  );

  const pollUntilDone = (rawInputId: string, toastId: string | number) => {
    const startedAt = Date.now();
    const tick = async () => {
      try {
        const input = await api.getInput(rawInputId);
        if (input.status === "processing") {
          if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
            toast.dismiss(toastId);
            toast.error("Task is taking longer than expected");
            return;
          }
          const id = window.setTimeout(() => {
            activePolls.current.delete(id);
            tick();
          }, POLL_INTERVAL_MS);
          activePolls.current.add(id);
          return;
        }
        toast.dismiss(toastId);
        const outcome = input.agent_trace?.outcome;
        if (input.task_id && outcome === "task_created") {
          toast.success("Task added");
        } else if (outcome === "task_creation_failed") {
          toast.error("Task creation failed");
        } else {
          toast.success("Done");
        }
        await onCreated();
      } catch (err) {
        toast.dismiss(toastId);
        toast.error((err as Error).message);
      }
    };
    void tick();
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const text = value.trim();
    if (!text || submitting) return;
    setSubmitting(true);
    try {
      const { raw_input_id } = await api.createTask(text);
      setValue("");
      const toastId = toast.loading("Creating task…", { duration: Infinity });
      pollUntilDone(raw_input_id, toastId);
    } catch (err) {
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
