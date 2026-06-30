import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import {
  CircleDot,
  ExternalLink,
  GitBranch,
  GitPullRequest,
  Pencil,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Markdown } from "@/components/ui/markdown";
import { Modal } from "@/components/ui/modal";
import { ModalSkeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { subjectLabel } from "@/components/runs/runLabels";
import { formatJsonLikeText } from "@/lib/format";
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
const LOG_PAGE_SIZE = 200;
const LOG_TOP_THRESHOLD = 8;

function load(
  task: KotxTask,
  doc: Props["doc"],
  view: Exclude<View, "log">,
): Promise<string | null> {
  if (view === "prompt") return kotx.getPrompt(task.id);
  return doc === "task" ? kotx.getBrief(task.id) : kotx.getReview(task.id);
}

function prependLogText(older: string | null, newer: string | null): string | null {
  if (!older) return newer;
  if (!newer) return older;
  const separator = older.endsWith("\n") || newer.startsWith("\n") ? "" : "\n";
  return `${older}${separator}${newer}`;
}

function branchUrl(task: KotxTask): string | null {
  if (!task.branch || !/^[^/\s]+\/[^/\s]+$/.test(task.repo)) return null;
  return `https://github.com/${task.repo}/tree/${encodeURIComponent(task.branch)}`;
}

export function RunDocModal({ task, doc, onClose, onChanged }: Props) {
  const primaryLabel = doc === "task" ? "TASK.md" : "REVIEW.md";
  // Resolve-conflict runs have no brief — drop the TASK.md tab and open on the
  // prompt instead.
  const showPrimary = task.kind !== "resolve_conflict";
  // kotx only accepts the PUT in the matching state; mirror that here so we
  // don't offer an Edit button that would 409.
  const canEditPrimary =
    (doc === "task" && task.state === "draft") ||
    (doc === "review" && task.state === "awaiting_approval");

  const [view, setView] = useState<View>(showPrimary ? "primary" : "prompt");
  const [content, setContent] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [logText, setLogText] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const [logLoadingMore, setLogLoadingMore] = useState(false);
  const [logHasMore, setLogHasMore] = useState(false);
  const [logBefore, setLogBefore] = useState<string | null>(null);
  const [logAtTop, setLogAtTop] = useState(false);
  const logScrollRef = useRef<HTMLDivElement>(null);
  const logScrollRestoreRef = useRef<
    "bottom" | { scrollHeight: number; scrollTop: number } | null
  >(null);

  useEffect(() => {
    if (view === "log") return;
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

  useEffect(() => {
    if (view !== "log") return;
    let cancelled = false;

    setEditing(false);
    setLogLoading(true);
    setLogLoadingMore(false);
    setLogText(null);
    setLogHasMore(false);
    setLogBefore(null);
    setLogAtTop(false);
    logScrollRestoreRef.current = "bottom";

    kotx
      .getLog(task.id, { tail: LOG_PAGE_SIZE })
      .then((page) => {
        if (cancelled) return;
        setLogText(page.text);
        setLogHasMore(page.hasMoreBefore);
        setLogBefore(page.before);
      })
      .catch((e) => {
        if (!cancelled) toast.error((e as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLogLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [task.id, view]);

  const displayedLog = view === "log" ? formatJsonLikeText(logText) : "";

  useLayoutEffect(() => {
    if (view !== "log" || logLoading) return;
    const el = logScrollRef.current;
    if (!el) return;

    const restore = logScrollRestoreRef.current;
    if (restore === "bottom") {
      el.scrollTop = el.scrollHeight;
    } else if (restore) {
      el.scrollTop = el.scrollHeight - restore.scrollHeight + restore.scrollTop;
    }
    logScrollRestoreRef.current = null;
    setLogAtTop(el.scrollTop <= LOG_TOP_THRESHOLD);
  }, [displayedLog, logLoading, view]);

  const handleLogScroll = () => {
    const el = logScrollRef.current;
    if (!el) return;
    const nextAtTop = el.scrollTop <= LOG_TOP_THRESHOLD;
    setLogAtTop((current) => (current === nextAtTop ? current : nextAtTop));
  };

  const loadOlderLog = async () => {
    if (logLoadingMore || !logHasMore || logBefore === null) return;

    const el = logScrollRef.current;
    logScrollRestoreRef.current = el
      ? { scrollHeight: el.scrollHeight, scrollTop: el.scrollTop }
      : null;
    setLogLoadingMore(true);
    try {
      const page = await kotx.getLog(task.id, {
        before: logBefore,
        limit: LOG_PAGE_SIZE,
      });
      if (!page.text) logScrollRestoreRef.current = null;
      setLogText((current) => prependLogText(page.text, current));
      setLogHasMore(page.hasMoreBefore);
      setLogBefore(page.before);
    } catch (e) {
      logScrollRestoreRef.current = null;
      toast.error((e as Error).message);
    } finally {
      setLogLoadingMore(false);
    }
  };

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
    ...(showPrimary ? [{ key: "primary" as const, label: primaryLabel }] : []),
    { key: "prompt", label: "Prompt" },
    { key: "log", label: "Log" },
  ];
  const taskBranchUrl = branchUrl(task);
  const taskSubjectLabel = subjectLabel(task);
  const SubjectIcon =
    task.subjectType === "pull_request" ? GitPullRequest : CircleDot;
  const logCanLoadMore = logHasMore && logBefore !== null;
  const activeLoading = view === "log" ? logLoading : loading;

  return (
    <Modal
      open
      onClose={onClose}
      title={taskSubjectLabel}
      titleClassName="text-lg"
      className="h-[760px] max-h-[calc(100dvh-2rem)] max-w-3xl"
    >
      <div className="mb-3 grid shrink-0 gap-2 rounded-lg border bg-muted/20 p-3 text-xs text-muted-foreground sm:grid-cols-2">
        <LinkMeta
          icon={<SubjectIcon className="h-3.5 w-3.5" />}
          label={task.repo}
          labelTitle={task.repo}
        >
          <a
            href={task.githubUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex min-w-0 max-w-full items-center gap-1 font-medium text-foreground hover:underline"
            title={taskSubjectLabel}
          >
            <span className="min-w-0 truncate">
              {taskSubjectLabel}
            </span>
            <ExternalLink className="h-3 w-3 shrink-0" />
          </a>
        </LinkMeta>
        <LinkMeta icon={<GitBranch className="h-3.5 w-3.5" />} label="Branch">
          {task.branch ? (
            taskBranchUrl ? (
              <a
                href={taskBranchUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex min-w-0 max-w-full items-center gap-1 font-mono text-foreground hover:underline"
                title={task.branch}
              >
                <span className="min-w-0 truncate">{task.branch}</span>
                <ExternalLink className="h-3 w-3 shrink-0" />
              </a>
            ) : (
              <span className="block min-w-0 truncate font-mono" title={task.branch}>
                {task.branch}
              </span>
            )
          ) : (
            <span>None</span>
          )}
        </LinkMeta>
      </div>

      <div className="mb-3 inline-flex shrink-0 rounded-lg bg-muted p-0.5 text-xs">
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

      <div className="min-h-0 flex-1 overflow-hidden">
        {activeLoading ? (
          <ModalSkeleton />
        ) : editing ? (
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="h-full resize-none font-mono text-xs leading-relaxed"
            autoFocus
          />
        ) : view === "log" ? (
          logText === null ? (
            <div className="flex h-full items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
              Not generated yet.
            </div>
          ) : (
            <div
              ref={logScrollRef}
              onScroll={handleLogScroll}
              className="h-full overflow-auto rounded-lg border bg-muted/40"
            >
              {logCanLoadMore && (
                <div
                  className={cn(
                    "sticky top-0 z-10 flex justify-center border-b bg-background/95 p-2",
                    !logAtTop && "invisible",
                  )}
                >
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={loadOlderLog}
                    disabled={logLoadingMore}
                  >
                    {logLoadingMore ? "Loading…" : "Load more"}
                  </Button>
                </div>
              )}
              <pre className="whitespace-pre-wrap break-words p-3 font-mono text-xs leading-relaxed">
                {displayedLog}
              </pre>
            </div>
          )
        ) : content === null ? (
          <div className="flex h-full items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
            Not generated yet.
          </div>
        ) : (
          <div className="h-full overflow-auto rounded-lg border p-3">
            <Markdown content={content ?? ""} />
          </div>
        )}
      </div>

      <div className="mt-3 flex shrink-0 items-center justify-end gap-2">
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

function LinkMeta({
  icon,
  label,
  labelTitle,
  children,
}: {
  icon: ReactNode;
  label: string;
  labelTitle?: string;
  children: ReactNode;
}) {
  return (
    <div className="grid min-w-0 grid-cols-[auto_minmax(0,7rem)_minmax(0,1fr)] items-center gap-2">
      <span className="shrink-0 text-muted-foreground">{icon}</span>
      <span className="min-w-0 truncate font-medium" title={labelTitle}>
        {label}
      </span>
      <span className="min-w-0 overflow-hidden">{children}</span>
    </div>
  );
}
