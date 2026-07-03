import { useEffect, useState } from "react";
import { CircleDot, GitBranch, GitMerge, GitPullRequest, Pencil } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Markdown } from "@/components/ui/markdown";
import { Textarea } from "@/components/ui/textarea";
import {
  isMergeProposal,
  isPrFollowUpRun,
  subjectLabel,
} from "@/components/runs/runLabels";
import { kotx, type KotxMergeContext, type KotxPr, type KotxTask } from "@/lib/kotx";
import { cn } from "@/lib/utils";

interface Props {
  task: KotxTask;
  onChanged: () => Promise<void> | void;
  // Called after a terminal action (approve/merge/comment/start) — the task
  // list should refresh since the kotx transition may close the 007 task.
  onActionDone: () => void;
}

// The consolidated task modal shows only the markdown views — prompt and log
// were dropped with the standalone runs modal.
type View = "primary" | "merge" | "pr";

function branchUrl(task: KotxTask): string | null {
  if (!task.branch || !/^[^/\s]+\/[^/\s]+$/.test(task.repo)) return null;
  return `https://github.com/${task.repo}/tree/${encodeURIComponent(task.branch)}`;
}

function subjectUrl(task: KotxTask): string {
  const prNumber =
    task.trackedPrNumber ??
    task.prNumber ??
    (task.subjectType === "pull_request" ? task.subjectNumber : null);
  if (prNumber && /^[^/\s]+\/[^/\s]+$/.test(task.repo)) {
    return `https://github.com/${task.repo}/pull/${prNumber}`;
  }
  return task.githubUrl;
}

export function KotxRunSection({ task, onChanged, onActionDone }: Props) {
  const doc: "task" | "review" = task.kind === "review" ? "review" : "task";
  const primaryLabel = doc === "task" ? "TASK.md" : "REVIEW.md";
  const mergeProposal = isMergeProposal(task);
  const prFollowUpRun = isPrFollowUpRun(task);
  const showPr = task.proposes === "pr" && !prFollowUpRun;
  const canEditPrimary =
    (doc === "task" && task.state === "draft") ||
    (doc === "review" && task.state === "awaiting_approval");

  const defaultView: View = mergeProposal ? "merge" : showPr ? "pr" : "primary";
  const [view, setView] = useState<View>(defaultView);
  const [content, setContent] = useState<string | null>(null);
  const [mergeContext, setMergeContext] = useState<KotxMergeContext | null>(null);
  const [draft, setDraft] = useState("");
  const [pr, setPr] = useState<KotxPr | null>(null);
  const [prTitleDraft, setPrTitleDraft] = useState("");
  const [prBodyDraft, setPrBodyDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setView(defaultView);
  }, [defaultView, task.id]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setEditing(false);
    if (view === "pr") {
      setPr(null);
    } else {
      setContent(null);
      setMergeContext(null);
      setDraft("");
    }
    const request =
      view === "pr"
        ? kotx.getPr(task.id).then((data) => {
            if (cancelled) return;
            setPr(data);
            setPrTitleDraft(data?.title ?? "");
            setPrBodyDraft(data?.body ?? "");
          })
        : view === "merge"
          ? kotx.getMergeContext(task.id).then((context) => {
              if (cancelled) return;
              setMergeContext(context);
              setContent(context?.commentMarkdown ?? null);
            })
          : (doc === "task" ? kotx.getBrief(task.id) : kotx.getReview(task.id)).then(
              (text) => {
                if (cancelled) return;
                setContent(text);
                setDraft(text ?? "");
              },
            );
    request
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
      if (done) onActionDone();
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

  const savePr = () =>
    withBusy(async () => {
      const next = { title: prTitleDraft, body: prBodyDraft };
      await kotx.putPr(task.id, next);
      setPr(next);
      setEditing(false);
    }, "PR saved");

  const views: { key: View; label: string }[] = [
    ...(mergeProposal ? [{ key: "merge" as const, label: "Approval" }] : []),
    ...(!showPr && !mergeProposal
      ? [{ key: "primary" as const, label: primaryLabel }]
      : []),
    ...(showPr ? [{ key: "pr" as const, label: "PR.md" }] : []),
  ];

  const taskBranchUrl = branchUrl(task);
  const SubjectIcon =
    task.subjectType === "pull_request" ? GitPullRequest : CircleDot;
  const approvedBy = mergeProposal ? mergeContext?.approvedBy?.trim() ?? "" : "";

  // Explicit manual follow-up: push the review feedback through a fresh
  // implement run on the tracked PR instead of merging as-is. This is the one
  // deliberate way a follow-up starts a new run — automatic triggers refresh
  // the existing kotx task instead.
  const mergeComment = view === "merge" ? content?.trim() ?? "" : "";
  const trackedPrNumber =
    task.trackedPrNumber ??
    task.prNumber ??
    mergeContext?.prNumber ??
    (task.subjectType === "pull_request" ? task.subjectNumber : null);
  const canStartPrFollowUp =
    mergeProposal && mergeComment.length > 0 && trackedPrNumber !== null;

  const startPrFollowUp = () =>
    withBusy(
      () =>
        kotx.createRun({
          type: "implement",
          repo: task.repo,
          number: trackedPrNumber ?? undefined,
        }),
      "Run started",
      true,
    );

  return (
    <section className="space-y-3">
      <div className="grid grid-cols-2 items-center gap-3 rounded-lg border bg-muted/20 p-3 text-xs text-muted-foreground">
        <a
          href={subjectUrl(task)}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex min-w-0 items-center gap-1.5 font-medium text-foreground hover:underline"
          title={subjectLabel(task)}
        >
          <SubjectIcon className="h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 truncate">{subjectLabel(task)}</span>
        </a>
        {task.branch ? (
          taskBranchUrl ? (
            <a
              href={taskBranchUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex min-w-0 items-center gap-1.5 font-medium text-foreground hover:underline"
              title={task.branch}
            >
              <GitBranch className="h-3.5 w-3.5 shrink-0" />
              <span className="min-w-0 truncate">{task.branch}</span>
            </a>
          ) : (
            <span
              className="inline-flex min-w-0 items-center gap-1.5 font-medium text-foreground"
              title={task.branch}
            >
              <GitBranch className="h-3.5 w-3.5 shrink-0" />
              <span className="min-w-0 truncate">{task.branch}</span>
            </span>
          )
        ) : (
          <span className="inline-flex min-w-0 items-center gap-1.5 font-medium">
            <GitBranch className="h-3.5 w-3.5 shrink-0" />
            None
          </span>
        )}
      </div>

      {views.length > 1 && (
        <div className="inline-flex rounded-lg bg-muted p-0.5 text-xs">
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
      )}

      <div className="max-h-80 min-h-24 overflow-auto rounded-lg border">
        {loading ? (
          <div className="p-3 text-sm text-muted-foreground">Loading…</div>
        ) : editing && view === "pr" ? (
          <div className="flex flex-col gap-2 p-2">
            <Input
              value={prTitleDraft}
              onChange={(e) => setPrTitleDraft(e.target.value)}
              placeholder="PR title"
              className="shrink-0 rounded-lg font-medium focus-visible:border-ring focus-visible:ring-0"
              autoFocus
            />
            <Textarea
              value={prBodyDraft}
              onChange={(e) => setPrBodyDraft(e.target.value)}
              placeholder="PR body"
              className="min-h-48 resize-y rounded-lg font-mono text-xs leading-relaxed focus-visible:border-ring focus-visible:ring-0"
            />
          </div>
        ) : editing ? (
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="min-h-64 w-full resize-y rounded-lg border-0 font-mono text-xs leading-relaxed focus-visible:ring-0"
            autoFocus
          />
        ) : view === "pr" ? (
          pr === null ? (
            <EmptyDoc />
          ) : (
            <div className="p-3">
              <div className="mb-3 border-b pb-3">
                <div className="mb-1 text-xs font-medium text-muted-foreground">
                  PR_TITLE.md
                </div>
                <div className="text-base font-semibold leading-snug">{pr.title}</div>
              </div>
              <div className="mb-1 text-xs font-medium text-muted-foreground">
                PR_BODY.md
              </div>
              <Markdown content={pr.body} />
            </div>
          )
        ) : view === "merge" ? (
          content?.trim() ? (
            <div className="p-3">
              <div className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <GitMerge className="h-3.5 w-3.5" />
                MERGE_APPROVAL.md
              </div>
              <Markdown content={content} />
            </div>
          ) : (
            <EmptyDoc label="No approval comment." />
          )
        ) : content === null ? (
          <EmptyDoc />
        ) : (
          <div className="p-3">
            <Markdown content={content} />
          </div>
        )}
      </div>

      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
          {approvedBy && (
            <span title={approvedBy}>
              Approved by:{" "}
              <span className="font-medium text-foreground">{approvedBy}</span>
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center justify-end gap-2">
          {!editing && content !== null && view === "primary" && canEditPrimary && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setEditing(true)}
              disabled={busy}
            >
              <Pencil className="h-3.5 w-3.5" />
              Edit
            </Button>
          )}
          {!editing && pr !== null && view === "pr" && showPr && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setEditing(true)}
              disabled={busy}
            >
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
                  if (view === "pr") {
                    setPrTitleDraft(pr?.title ?? "");
                    setPrBodyDraft(pr?.body ?? "");
                  } else {
                    setDraft(content ?? "");
                  }
                  setEditing(false);
                }}
                disabled={busy}
              >
                Cancel
              </Button>
              <Button size="sm" onClick={view === "pr" ? savePr : save} disabled={busy}>
                Save
              </Button>
            </>
          )}
          {!editing && view === "merge" && mergeProposal && (
            <>
              {mergeComment.length > 0 && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={startPrFollowUp}
                  disabled={busy || !canStartPrFollowUp}
                  title={
                    trackedPrNumber === null
                      ? "No tracked pull request is available"
                      : undefined
                  }
                >
                  Run with feedback
                </Button>
              )}
              <Button
                size="sm"
                onClick={() => withBusy(() => kotx.merge(task.id), "Merged", true)}
                disabled={busy}
              >
                Merge
              </Button>
            </>
          )}
          {!editing && view !== "merge" && (
            <>
              {task.canComment && (
                <Button
                  size="sm"
                  onClick={() =>
                    withBusy(() => kotx.comment(task.id), "Comment posted", true)
                  }
                  disabled={busy || !content?.trim()}
                >
                  Comment
                </Button>
              )}
              {task.canStart && (
                <Button
                  size="sm"
                  onClick={() => withBusy(() => kotx.start(task.id), "Started", true)}
                  disabled={busy}
                >
                  Start
                </Button>
              )}
              {task.canApprove && !prFollowUpRun && !mergeProposal && (
                <Button
                  variant={task.proposes === "pr" ? "default" : "outline"}
                  size="sm"
                  onClick={() =>
                    withBusy(
                      () => kotx.approve(task.id),
                      task.proposes === "pr" ? "PR opened" : "Approved",
                      true,
                    )
                  }
                  disabled={busy}
                >
                  {task.proposes === "pr" ? "Open PR" : "Approve"}
                </Button>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}

function EmptyDoc({ label = "Not generated yet." }: { label?: string }) {
  return (
    <div className="flex min-h-24 items-center justify-center text-sm text-muted-foreground">
      {label}
    </div>
  );
}
