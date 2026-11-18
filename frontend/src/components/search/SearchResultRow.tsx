import { CalendarDays, FileText, Github, Inbox, ListTodo, NotebookPen, UserRound } from "lucide-react";
import { useCallback, useEffect, useState, type ComponentType } from "react";
import { TaskCard } from "@/components/tasks/TaskCard";
import { InboxCard } from "@/components/inbox/InboxCard";
import { NoteCard } from "@/components/notes/NoteCard";
import { ContactCard, DocCard, EventCard } from "@/components/search/AssistantContent";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { useLabels } from "@/hooks/useLabels";
import { fmtWhen } from "@/lib/dates";
import { api } from "@/lib/api";
import { badgeKindLabel, displaySender, sourceLabel } from "@/lib/inbox";
import { labelChipStyle } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { ChatCitation, Note, RawInput, SearchHit, SearchHitType, Task } from "@/lib/types";

const TYPE_ICON: Record<
  SearchHitType,
  ComponentType<{ className?: string }>
> = {
  task: ListTodo,
  input: Inbox,
  note: NotebookPen,
  document: FileText,
  drive: FileText,
  contact: UserRound,
  github: Github,
};

function hitIcon(hit: SearchHit): ComponentType<{ className?: string }> {
  if (hit.type === "document" && hit.source === "calendar") return CalendarDays;
  return TYPE_ICON[hit.type] ?? Inbox;
}

// Reuse the inbox status badges verbatim; `event`/`processing` have no inbox
// variant, so fall back to a muted pill.
const STATUS_VARIANT: Record<string, BadgeProps["variant"]> = {
  open: "open",
  closed: "closed",
  not_task: "not_task",
  duplicate: "duplicate",
  reopened: "reopened",
  updated: "updated",
  no_change: "no_change",
  event: "muted",
  processing: "muted",
};

// Second line under the title follows the task card's compact pill layout.
function metaPills(hit: SearchHit): string[] {
  return [
    hit.meta?.start || hit.ts ? fmtWhen(hit.meta?.start ?? hit.ts) : null,
    hit.sender ? displaySender(hit.sender) : null,
    hit.meta?.location ?? hit.meta?.mime ?? (hit.source ? sourceLabel(hit.source) : null),
  ]
    .filter((value): value is string => Boolean(value));
}

// Almost every message is "no task", so in search that badge is just noise.
function showsStatus(hit: SearchHit): hit is SearchHit & { status: string } {
  return Boolean(hit.status) && !(hit.type === "input" && hit.status === "not_task");
}

export function SearchResultRow({
  hit,
  task,
  input,
  onChanged,
  onOpenTask,
  onShowContent,
  preventBlur = false,
}: {
  hit: SearchHit;
  task?: Task;
  input?: RawInput;
  onChanged?: () => Promise<void> | void;
  onOpenTask: (taskId: string) => void;
  // Optional fallback for hits with no task or URL, used by chat citation cards.
  onShowContent?: () => void;
  // Keep focus in an embedding input when a row is clicked.
  preventBlur?: boolean;
}) {
  if (hit.type === "task") {
    return <TaskSuggestionCard
      hit={hit} initialTask={task} onOpenTask={onOpenTask} preventBlur={preventBlur}
    />;
  }
  if (hit.type === "input" && input) {
    return <div onMouseDown={preventBlur ? (event) => event.preventDefault() : undefined}>
      <InboxCard
        item={{ id: input.id, sort: input.received_at, data: input }}
        onChanged={onChanged ?? (() => {})}
        unseen={false}
        onVisible={() => {}}
        onOpenTask={onOpenTask}
        hideNotTaskBadge
      />
    </div>;
  }
  if (hit.type === "note") {
    return <div onMouseDown={preventBlur ? (event) => event.preventDefault() : undefined}>
      <NoteSuggestionCard noteId={hit.id} />
    </div>;
  }
  if (hit.type === "contact" || hit.type === "drive"
      || (hit.type === "document" && !hit.task_id)) {
    const cite: ChatCitation = {
      ...hit,
      tag: "",
      url: hit.url ?? (hit.type === "drive"
        ? `https://drive.google.com/open?id=${encodeURIComponent(hit.id)}`
        : null),
    };
    return (
      <div onMouseDown={preventBlur ? (e) => e.preventDefault() : undefined}>
        {hit.type === "contact"
          ? <ContactCard cite={cite} />
          : hit.source === "calendar"
            ? <EventCard cite={cite} onShowContent={onShowContent} />
            : <DocCard cite={cite} onShowContent={onShowContent} />}
      </div>
    );
  }
  return <CompactResultRow
    hit={hit} onOpenTask={onOpenTask} onShowContent={onShowContent} preventBlur={preventBlur}
  />;
}

function TaskSuggestionCard({
  hit, initialTask, onOpenTask, preventBlur,
}: {
  hit: SearchHit;
  initialTask?: Task;
  onOpenTask: (taskId: string) => void;
  preventBlur: boolean;
}) {
  const [task, setTask] = useState<Task | null>(initialTask ?? null);
  const refresh = useCallback(async () => {
    setTask(await api.getTask(hit.id));
  }, [hit.id]);

  if (!task) return null;

  return (
    <div onMouseDown={preventBlur ? (e) => e.preventDefault() : undefined}>
      <TaskCard
        task={task}
        compact
        kotxTask={null}
        onChanged={refresh}
        onKotxChanged={refresh}
        onOpen={onOpenTask}
      />
    </div>
  );
}

// The same note card the notes list shows. A hit carries only a snippet and
// editing needs the full text, so the note is fetched before it renders.
function NoteSuggestionCard({ noteId }: { noteId: string }) {
  const [note, setNote] = useState<Note | null>(null);
  const [gone, setGone] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.getNote(noteId)
      .then((loaded) => { if (!cancelled) setNote(loaded); })
      .catch(() => { if (!cancelled) setGone(true); });
    return () => { cancelled = true; };
  }, [noteId]);

  if (gone) return null;
  if (!note) return <div className="h-[76px] animate-pulse rounded-xl border bg-muted/40" />;
  return <NoteCard note={note} onSaved={setNote} onDeleted={() => setGone(true)} />;
}

function CompactResultRow({
  hit,
  onOpenTask,
  onShowContent,
  preventBlur = false,
}: {
  hit: SearchHit;
  onOpenTask: (taskId: string) => void;
  onShowContent?: () => void;
  preventBlur?: boolean;
}) {
  const Icon = hitIcon(hit);
  const openTask = hit.task_id ?? (hit.type === "task" ? hit.id : null);
  const openUrl = !openTask
    ? hit.url ?? (hit.type === "drive"
      ? `https://drive.google.com/open?id=${encodeURIComponent(hit.id)}`
      : null)
    : null;
  const clickable = Boolean(openTask || openUrl || onShowContent);
  const pills = metaPills(hit);
  const labels = useLabels();
  const repoLabel = hit.type === "github" && typeof hit.meta?.label === "string" ? hit.meta.label : null;
  const repoLabelMeta = repoLabel ? labels.find((label) => label.name === repoLabel) : undefined;

  const activate = () => {
    if (openTask) onOpenTask(openTask);
    else if (openUrl) window.open(openUrl, "_blank", "noopener,noreferrer");
    else onShowContent?.();
  };

  return (
    <button
      type="button"
      disabled={!clickable}
      onMouseDown={preventBlur ? (e) => e.preventDefault() : undefined}
      onClick={clickable ? activate : undefined}
      className={cn(
        "flex h-[76px] w-full items-center gap-2 overflow-hidden rounded-xl border bg-card p-3 pl-2 text-left shadow-sm",
        clickable ? "cursor-pointer" : "cursor-default",
      )}
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center text-muted-foreground" aria-hidden="true">
        <Icon className="h-5 w-5" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-base font-medium leading-snug">
          {hit.title || "Untitled"}
        </span>
        <span className="mt-1 flex min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap">
          {showsStatus(hit) && (
            <Badge variant={STATUS_VARIANT[hit.status] ?? "muted"} className="shrink-0">
              {badgeKindLabel(hit.status)}
            </Badge>
          )}
          {repoLabel && (
            <span
              title={repoLabelMeta?.description || repoLabel}
              className="inline-flex shrink-0 items-center rounded-full border border-transparent bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground"
              style={labelChipStyle(repoLabelMeta?.color)}
            >
              {repoLabel}
            </span>
          )}
          {pills.map((pill, index) => (
            <span key={`${pill}:${index}`} title={pill} className="max-w-[50%] shrink-0 truncate rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
              {pill}
            </span>
          ))}
        </span>
      </span>
    </button>
  );
}
