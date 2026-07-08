import { CalendarDays, FileText, Inbox, ListTodo } from "lucide-react";
import type { ComponentType } from "react";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { fmtWhen } from "@/lib/dates";
import { badgeKindLabel } from "@/lib/inbox";
import { cn } from "@/lib/utils";
import type { SearchHit, SearchHitType } from "@/lib/types";

const TYPE_ICON: Record<SearchHitType, ComponentType<{ className?: string }>> = {
  task: ListTodo,
  input: Inbox,
  note: FileText,
  document: FileText,
  drive: FileText,
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

// Second line under the title: sender · date · source (whichever exist).
function metaLine(hit: SearchHit): string {
  return [
    hit.sender ? displaySender(hit.sender) : null,
    hit.ts ? fmtWhen(hit.ts) : null,
    hit.source ? capitalize(hit.source) : null,
  ]
    .map((p) => (p ?? "").trim())
    .filter(Boolean)
    .join(" · ");
}

export function SearchResultRow({
  hit,
  onOpenTask,
  onActivate,
  onShowContent,
  preventBlur = false,
}: {
  hit: SearchHit;
  onOpenTask: (taskId: string) => void;
  // Fired after any successful activation — the composer uses it to dismiss
  // its dropdown.
  onActivate?: () => void;
  // Optional fallback for hits with no task or URL, used by chat citation cards.
  onShowContent?: () => void;
  // In the composer the input must keep focus when a row is clicked.
  preventBlur?: boolean;
}) {
  const Icon = hitIcon(hit);
  const openTask = hit.task_id ?? (hit.type === "task" ? hit.id : null);
  const openUrl = !openTask && hit.url ? hit.url : null;
  const clickable = Boolean(openTask || openUrl || onShowContent);
  const meta = metaLine(hit);

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
        "flex w-full items-start gap-3 rounded-xl border bg-card px-3 py-2.5 text-left shadow-sm transition-colors",
        clickable
          ? "cursor-pointer hover:border-primary/40 hover:bg-accent hover:text-accent-foreground"
          : "cursor-default",
      )}
    >
      <Icon className="h-4 w-4 shrink-0 self-center text-muted-foreground" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{hit.title || "Untitled"}</span>
        {meta && (
          <span className="mt-0.5 block truncate text-xs text-muted-foreground">{meta}</span>
        )}
      </span>
      {hit.status && (
        <Badge variant={STATUS_VARIANT[hit.status] ?? "muted"} className="mt-0.5 shrink-0">
          {badgeKindLabel(hit.status)}
        </Badge>
      )}
    </button>
  );
}
