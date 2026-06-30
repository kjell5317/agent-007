import { useEffect, useState } from "react";
import { GitBranch, Pencil } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Markdown } from "@/components/ui/markdown";
import { Modal } from "@/components/ui/modal";
import { Textarea } from "@/components/ui/textarea";
import { kotx, type KotxTask } from "@/lib/kotx";
import { cn } from "@/lib/utils";

interface Props {
  task: KotxTask;
  // The run's primary document: TASK.md for implement/conflict runs, REVIEW.md
  // for review runs. Prompt and Log are always available as read-only views.
  doc: "task" | "review";
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}

type View = "primary" | "prompt" | "log";

function load(task: KotxTask, doc: Props["doc"], view: View): Promise<string | null> {
  if (view === "prompt") return kotx.getPrompt(task.id);
  if (view === "log") return kotx.getLog(task.id);
  return doc === "task" ? kotx.getBrief(task.id) : kotx.getReview(task.id);
}

export function RunDocModal({ task, doc, onClose, onChanged }: Props) {
  const primaryLabel = doc === "task" ? "TASK.md" : "REVIEW.md";
  // kotx only accepts the PUT in the matching state; mirror that here so we
  // don't offer an Edit button that would 409.
  const canEditPrimary =
    (doc === "task" && task.state === "draft") ||
    (doc === "review" && task.state === "awaiting_approval");

  const [view, setView] = useState<View>("primary");
  const [content, setContent] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setEditing(false);
    load(task, doc, view)
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
  }, [task.id, doc, view]);

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
      const put = doc === "task" ? kotx.putBrief : kotx.putReview;
      await put(task.id, draft);
      setContent(draft);
      setEditing(false);
    }, `${primaryLabel} saved`);

  const views: { key: View; label: string }[] = [
    { key: "primary", label: primaryLabel },
    { key: "prompt", label: "Prompt" },
    { key: "log", label: "Log" },
  ];

  return (
    <Modal
      open
      onClose={onClose}
      title={`${task.repo} #${task.subjectNumber}`}
      className="max-w-2xl"
    >
      {task.branch && (
        <div className="mb-3 flex items-center gap-1.5 text-xs text-muted-foreground">
          <GitBranch className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate font-mono" title={task.branch}>
            {task.branch}
          </span>
        </div>
      )}

      <div className="mb-3 inline-flex rounded-lg bg-muted p-0.5 text-xs">
        {views.map((v) => (
          <button
            key={v.key}
            type="button"
            onClick={() => setView(v.key)}
            disabled={busy}
            className={cn(
              "rounded-md px-2.5 py-1 font-medium transition-colors",
              view === v.key
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {v.label}
          </button>
        ))}
      </div>

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
      ) : view === "log" ? (
        // Raw Codex transcript is JSONL, not markdown — show it verbatim.
        <pre className="max-h-[55vh] overflow-auto whitespace-pre-wrap break-words rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
          {content}
        </pre>
      ) : (
        <div className="max-h-[55vh] overflow-auto rounded-lg border p-3">
          <Markdown content={content ?? ""} />
        </div>
      )}

      <div className="mt-3 flex items-center justify-end gap-2">
        {view === "primary" && canEditPrimary && !editing && content !== null && (
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
        {!editing && view === "primary" && doc === "task" && task.canStart && (
          <Button
            size="sm"
            onClick={() => withBusy(() => kotx.start(task.id), "Started", true)}
            disabled={busy}
          >
            Start
          </Button>
        )}
        {!editing && view === "primary" && doc === "review" && task.canApprove && (
          <Button
            size="sm"
            onClick={() => withBusy(() => kotx.approve(task.id), "Comment posted", true)}
            disabled={busy}
          >
            Comment
          </Button>
        )}
      </div>
    </Modal>
  );
}
