import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";

interface Props {
  onCreated: () => Promise<void> | void;
}

export function Composer({ onCreated }: Props) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const text = value.trim();
    if (!text) return;
    setBusy(true);
    try {
      await api.createTask(text);
      setValue("");
      toast.success("Task added");
      await onCreated();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
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
        <Button type="submit" disabled={busy || !value.trim()} className="rounded-full px-5">
          Add
        </Button>
      </div>
    </form>
  );
}
