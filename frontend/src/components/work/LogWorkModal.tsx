import { useState, type FormEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LabelPicker } from "@/components/ui/label-picker";
import { Modal } from "@/components/ui/modal";
import { useLabels } from "@/hooks/useLabels";
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

// Complete on a task that was never started: ask when the work happened, but
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

// Work that was never a task: creates it, logs the span and closes it.
export function LogWorkModal({
  onClose,
  onLogged,
}: {
  onClose: () => void;
  onLogged: () => Promise<void> | void;
}) {
  const labels = useLabels();
  const [title, setTitle] = useState("");
  const [label, setLabel] = useState("");
  const { span, fields } = useSpanDraft();
  const { busy, error, run } = useSubmitting();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!span || !title.trim()) return;
    void run(async () => {
      await api.logSpontaneousWork({ title: title.trim(), ...span, ...(label ? { label } : {}) });
      toast.success("Work logged");
      onClose();
      void Promise.resolve(onLogged()).catch(() => {});
    });
  };

  return (
    <Modal open onClose={onClose} title="Log work">
      <form onSubmit={submit} className="space-y-4" autoComplete="off">
        <Field id="work-title" label="What did you do?">
          <Input
            id="work-title"
            autoFocus
            required
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            autoCapitalize="sentences"
            className="h-10"
          />
        </Field>
        {fields}
        <div className="space-y-1.5">
          <span className={FIELD_LABEL_CLASS}>Label</span>
          <LabelPicker value={label} onChange={setLabel} labels={labels} />
        </div>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <div className="flex gap-2">
          <Button type="button" variant="outline" className="flex-1" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" className="flex-1" disabled={busy || !span || !title.trim()}>
            Log
          </Button>
        </div>
      </form>
    </Modal>
  );
}
