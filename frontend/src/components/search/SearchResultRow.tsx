import { CalendarDays, FileText, Inbox, ListTodo } from "lucide-react";
import type { ComponentType } from "react";
import { fmtWhen } from "@/lib/dates";
import { cn } from "@/lib/utils";
import type { SearchHit, SearchHitType } from "@/lib/types";

const TYPE_ICON: Record<SearchHitType, ComponentType<{ className?: string }>> = {
  task: ListTodo,
  input: Inbox,
  note: FileText,
  document: FileText,
};

function hitIcon(hit: SearchHit): ComponentType<{ className?: string }> {
  if (hit.type === "document" && hit.source === "calendar") return CalendarDays;
  return TYPE_ICON[hit.type] ?? Inbox;
}

// A distinct tint per status so the badge reads at a glance.
const STATUS_STYLE: Record<string, string> = {
  open: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  closed: "bg-zinc-500/15 text-zinc-600 dark:text-zinc-300",
  done: "bg-zinc-500/15 text-zinc-600 dark:text-zinc-300",
  not_task: "bg-zinc-500/15 text-zinc-600 dark:text-zinc-300",
  duplicate: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  processing: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
  event: "bg-violet-500/15 text-violet-700 dark:text-violet-300",
};

// "Alice <a@x.com>" → "Alice" (mirrors lib/inbox senderName, which needs a
// RawInput; here we only have the raw `from` string).
function displaySender(from: string): string {
  const m = from.match(/^"?([^"<]*?)"?\s*<([^>]+)>$/);
  const name = m ? m[1].trim() || m[2].trim() : from;
  return name.replace(/\s*\([^)]*\)\s*$/, "").trim() || name;
}

// Second line under the title: sender · date · source (whichever exist).
function metaLine(hit: SearchHit): string {
  return [
    hit.sender ? displaySender(hit.sender) : null,
    hit.ts ? fmtWhen(hit.ts) : null,
    hit.source,
  ]
    .map((p) => (p ?? "").trim())
    .filter(Boolean)
    .join(" · ");
}

export function SearchResultRow({
  hit,
  onOpenTask,
  onActivate,
  preventBlur = false,
}: {
  hit: SearchHit;
  onOpenTask: (taskId: string) => void;
  // Fired after any successful activation (task open or url) — the composer
  // uses it to dismiss its dropdown.
  onActivate?: () => void;
  // In the composer the input must keep focus when a row is clicked.
  preventBlur?: boolean;
}) {
  const Icon = hitIcon(hit);
  const openTask = hit.task_id;
  const openUrl = !openTask && hit.url ? hit.url : null;
  const clickable = Boolean(openTask || openUrl);
  const meta = metaLine(hit);

  const activate = () => {
    if (openTask) onOpenTask(openTask);
    else if (openUrl) window.open(openUrl, "_blank", "noopener,noreferrer");
    onActivate?.();
  };

  return (
    <button
      type="button"
      disabled={!clickable}
      onMouseDown={preventBlur ? (e) => e.preventDefault() : undefined}
      onClick={clickable ? activate : undefined}
      className={cn(
        "flex w-full items-start gap-3 rounded-xl border bg-card px-3 py-2.5 text-left shadow-sm transition-colors",
        clickable
          ? "cursor-pointer hover:border-primary/40 hover:bg-accent hover:text-accent-foreground"
          : "cursor-default",
      )}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{hit.title || "Untitled"}</span>
        {meta && (
          <span className="mt-0.5 block truncate text-xs text-muted-foreground">{meta}</span>
        )}
      </span>
      {hit.status && (
        <span
          className={cn(
            "mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium capitalize",
            STATUS_STYLE[hit.status] ?? "bg-secondary text-muted-foreground",
          )}
        >
          {hit.status.replace(/_/g, " ")}
        </span>
      )}
    </button>
  );
}
