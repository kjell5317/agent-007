import { useState, type FormEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { api, type WorkSpan } from "@/lib/api";
import { fromZonedInputValue, toZonedInputValue } from "@/lib/dates";
import { toast } from "@/lib/toast";

const DEFAULT_SPAN_MS = 30 * 60_000;
const FIELD_LABEL_CLASS = "text-xs font-semibold uppercase tracking-wider text-muted-foreground";

function useSpanDraft() {
  const [start, setStart] = useState(() => toZonedInputValue(new Date(Date.now() - DEFAULT_SPAN_MS)));
  const [end, setEnd] = useState(() => toZonedInputValue(new Date()));
  const startedAt = fromZonedInputValue(start);
  const endedAt = fromZonedInputValue(end);
  const span: WorkSpan | null = startedAt && endedAt ? { started_at: startedAt, ended_at: endedAt } : null;
  const fields = (
    <>
      <Field id="work-start" label="Start">
        <Input id="work-start" type="datetime-local" required value={start} onChange={(e) => setStart(e.target.value)} className="h-10" />
      </Field>
      <Field id="work-end" label="End">
        <Input id="work-end" type="datetime-local" required value={end} onChange={(e) => setEnd(e.target.value)} className="h-10" />
      </Field>
    </>
  );
  return { span, fields };
}

function Field({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className={FIELD_LABEL_CLASS}>{label}</label>
      {children}
    </div>
  );
}

function useSubmitting() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (action: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run };
}

// Complete with work done outside a live session: ask when it happened, but
// never block closing it.
export function CompleteLogSheet({
  onLog,
  onUnknown,
  onClose,
}: {
  onLog: (span: WorkSpan) => Promise<unknown>;
  onUnknown: () => Promise<unknown>;
  onClose: () => void;
}) {
  const { span, fields } = useSpanDraft();
  const { busy, error, run } = useSubmitting();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (span) void run(() => onLog(span));
  };

  return (
    <Modal open onClose={onClose} title="When did you work on it?">
      <form onSubmit={submit} className="space-y-4">
        {fields}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <div className="flex gap-2">
          <Button type="button" variant="outline" className="flex-1" disabled={busy} onClick={() => void run(onUnknown)}>
            Don't know
          </Button>
          <Button type="submit" className="flex-1" disabled={busy || !span}>
            Complete
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// Work that was never a task: creates it, logs the span and closes it. The
// title and label come from the composer.
export function LogWorkModal({
  title,
  label,
  onClose,
  onLogged,
}: {
  title: string;
  label: string | null;
  onClose: () => void;
  onLogged: () => void;
}) {
  const { span, fields } = useSpanDraft();
  const { busy, error, run } = useSubmitting();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (span) void run(async () => {
      await api.logSpontaneousWork({ title, ...span, ...(label ? { label } : {}) });
      toast.success("Work logged");
      onLogged();
    });
  };

  return (
    <Modal open onClose={onClose} title="Log work">
      <form onSubmit={submit} className="space-y-4" autoComplete="off">
        <p className="line-clamp-2 break-words text-sm font-medium" title={title}>{title}</p>
        {fields}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <div className="flex gap-2">
          <Button type="button" variant="outline" className="flex-1" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" className="flex-1" disabled={busy || !span}>
            Log
          </Button>
        </div>
      </form>
    </Modal>
  );
}
