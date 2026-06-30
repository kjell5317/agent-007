import { useEffect, useState } from "react";
import { GitBranch, Pencil } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/modal";
import { Textarea } from "@/components/ui/textarea";
import { kotx, type KotxTask } from "@/lib/kotx";

interface Props {
  task: KotxTask;
  doc: "task" | "review";
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}

// TASK.md is editable only while the run is a draft (kotx rejects the PUT
// otherwise); REVIEW.md is always read-only.
export function RunDocModal({ task, doc, onClose, onChanged }: Props) {
  const editable = doc === "task" && task.state === "draft";
  const [content, setContent] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const load = doc === "task" ? kotx.getBrief(task.id) : kotx.getReview(task.id);
    load
      .then((text) => {
        if (cancelled) return;
        setContent(text);
        setDraft(text ?? "");
      })
      .catch((e) => {
        if (!cancelled) toast.error((e as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [task.id, doc]);

  async function withBusy<T>(fn: () => Promise<T>, msg: string, done?: boolean) {
    setBusy(true);
    try {
      await fn();
      toast.success(msg);
      await onChanged();
      if (done) onClose();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const save = () =>
    withBusy(async () => {
      await kotx.putBrief(task.id, draft);
      setContent(draft);
      setEditing(false);
    }, "Brief saved");

  const title = doc === "task" ? `TASK.md · #${task.subjectNumber}` : `REVIEW.md · #${task.subjectNumber}`;

  return (
    <Modal open onClose={onClose} title={title} className="max-w-2xl">
      {task.branch && (
        <div className="mb-3 flex items-center gap-1.5 text-xs text-muted-foreground">
          <GitBranch className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate font-mono" title={task.branch}>
            {task.branch}
          </span>
        </div>
      )}
      {loading ? (
        <div className="py-8 text-center text-sm text-muted-foreground">Loading…</div>
      ) : content === null && !editing ? (
        <div className="rounded-lg border border-dashed py-8 text-center text-sm text-muted-foreground">
          Not generated yet.
        </div>
      ) : editing ? (
        <Textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className="h-[55vh] resize-none font-mono text-xs leading-relaxed"
          autoFocus
        />
      ) : (
        <pre className="max-h-[55vh] overflow-auto whitespace-pre-wrap break-words rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
          {content}
        </pre>
      )}

      <div className="mt-3 flex items-center justify-end gap-2">
        {editable && !editing && (
          <Button variant="outline" size="sm" onClick={() => setEditing(true)} disabled={busy}>
            <Pencil className="h-3.5 w-3.5" />
            Edit
          </Button>
        )}
        {editing && (
          <>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setDraft(content ?? "");
                setEditing(false);
              }}
              disabled={busy}
            >
              Cancel
            </Button>
            <Button size="sm" onClick={save} disabled={busy}>
              Save
            </Button>
          </>
        )}
        {!editing && doc === "task" && task.canStart && (
          <Button
            size="sm"
            onClick={() => withBusy(() => kotx.start(task.id), "Started", true)}
            disabled={busy}
          >
            Start
          </Button>
        )}
        {!editing && doc === "review" && task.canApprove && (
          <Button
            size="sm"
            onClick={() => withBusy(() => kotx.approve(task.id), "Approved", true)}
            disabled={busy}
          >
            Approve
          </Button>
        )}
      </div>
    </Modal>
  );
}
