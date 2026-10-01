import { CalendarDays, FileText, GitPullRequest, Inbox, ListTodo, UserRound } from "lucide-react";
import { useCallback, useState, type ComponentType } from "react";
import { TaskCard } from "@/components/tasks/TaskCard";
import { ContactCard, DocCard, EventCard } from "@/components/search/AssistantContent";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { fmtWhen } from "@/lib/dates";
import { api } from "@/lib/api";
import { badgeKindLabel } from "@/lib/inbox";
import { cn } from "@/lib/utils";
import type { ChatCitation, SearchHit, SearchHitType, Task } from "@/lib/types";

const TYPE_ICON: Record<
  SearchHitType,
  ComponentType<{ className?: string }>
> = {
  task: ListTodo,
  input: Inbox,
  note: FileText,
  document: FileText,
  drive: FileText,
  contact: UserRound,
  github: GitPullRequest,
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

// "Alice <a@x.com>" → "Alice" (mirrors lib/inbox senderName, which needs a
// RawInput; here we only have the raw `from` string).
function displaySender(from: string): string {
  const m = from.match(/^"?([^"<]*?)"?\s*<([^>]+)>$/);
  const name = m ? m[1].trim() || m[2].trim() : from;
  return name.replace(/\s*\([^)]*\)\s*$/, "").trim() || name;
}

function capitalize(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

// Second line under the title follows the task card's compact pill layout.
function metaPills(hit: SearchHit): string[] {
  if (hit.type === "contact") {
    return [hit.meta?.org, hit.meta?.emails?.[0], hit.meta?.phones?.[0]]
      .filter((value): value is string => Boolean(value));
  }
  return [
    hit.sender ? displaySender(hit.sender) : null,
    hit.meta?.start || hit.ts ? fmtWhen(hit.meta?.start ?? hit.ts) : null,
    hit.meta?.location ?? hit.meta?.mime ?? (hit.source ? capitalize(hit.source) : null),
  ]
    .filter((value): value is string => Boolean(value));
}

export function SearchResultRow({
  hit,
  task,
  onOpenTask,
  onActivate,
  onShowContent,
  preventBlur = false,
}: {
  hit: SearchHit;
  task?: Task;
  onOpenTask: (taskId: string) => void;
  // Fired after activation when embedded in another view.
  onActivate?: () => void;
  // Optional fallback for hits with no task or URL, used by chat citation cards.
  onShowContent?: () => void;
  // Keep focus in an embedding input when a row is clicked.
  preventBlur?: boolean;
}) {
  if (hit.type === "task") {
    return <TaskSuggestionCard
      hit={hit} initialTask={task} onOpenTask={onOpenTask} onActivate={onActivate}
      preventBlur={preventBlur}
    />;
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
          ? <ContactCard cite={cite} onOpened={onActivate} />
          : hit.source === "calendar"
            ? <EventCard cite={cite} onShowContent={onShowContent} onOpened={onActivate} />
            : <DocCard cite={cite} onShowContent={onShowContent} onOpened={onActivate} />}
      </div>
    );
  }
  return <CompactResultRow
    hit={hit} onOpenTask={onOpenTask} onActivate={onActivate}
    onShowContent={onShowContent} preventBlur={preventBlur}
  />;
}

function TaskSuggestionCard({
  hit, initialTask, onOpenTask, onActivate, preventBlur,
}: {
  hit: SearchHit;
  initialTask?: Task;
  onOpenTask: (taskId: string) => void;
  onActivate?: () => void;
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
        onOpen={(id) => { onOpenTask(id); onActivate?.(); }}
      />
    </div>
  );
}

function CompactResultRow({
  hit,
  onOpenTask,
  onActivate,
  onShowContent,
  preventBlur = false,
}: {
  hit: SearchHit;
  onOpenTask: (taskId: string) => void;
  onActivate?: () => void;
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

  const activate = () => {
    if (openTask) onOpenTask(openTask);
    else if (openUrl) window.open(openUrl, "_blank", "noopener,noreferrer");
    else onShowContent?.();
    onActivate?.();
  };

  return (
    <button
      type="button"
      disabled={!clickable}
      onMouseDown={preventBlur ? (e) => e.preventDefault() : undefined}
      onClick={clickable ? activate : undefined}
      className={cn(
        "flex h-[76px] w-full items-center gap-2 overflow-hidden rounded-xl border bg-card p-3 pl-2 text-left shadow-sm transition-colors",
        clickable
          ? "cursor-pointer hover:border-primary/40 hover:bg-accent hover:text-accent-foreground"
          : "cursor-default",
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
          {hit.status && (
            <Badge variant={STATUS_VARIANT[hit.status] ?? "muted"} className="shrink-0">
              {badgeKindLabel(hit.status)}
            </Badge>
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
