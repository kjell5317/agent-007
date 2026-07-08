import {
  CalendarDays,
  Check,
  ChevronDown,
  ExternalLink,
  FileText,
  HardDrive,
  Inbox,
  ListTodo,
  Loader2,
  Plus,
  Wrench,
  X,
} from "lucide-react";
import type { ComponentType } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { AssistantContent } from "@/components/search/AssistantContent";
import { fmtWhen } from "@/lib/dates";
import { cn } from "@/lib/utils";
import type { ChatCitation, ChatMessage, ChatToolTrace } from "@/lib/types";

const TYPE_ICON: Record<string, ComponentType<{ className?: string }>> = {
  task: ListTodo,
  input: Inbox,
  note: FileText,
  document: CalendarDays,
  drive: HardDrive,
};

function citationIcon(cite: ChatCitation): ComponentType<{ className?: string }> {
  if (cite.type === "document" && cite.source === "calendar") return CalendarDays;
  return TYPE_ICON[cite.type] ?? FileText;
}

// Citation tags the answer actually referenced ([T1] inline or task:{id} widget).
function usedTags(message: ChatMessage): Set<string> {
  const used = new Set<string>();
  for (const m of message.content.matchAll(/\[([A-Z]\d+)\]/g)) used.add(m[1]);
  const taskRefs = new Set(
    [...message.content.matchAll(/task:\{([^}]+)\}/g)].map((m) => m[1].trim()),
  );
  if (taskRefs.size) {
    for (const c of message.citations) {
      if (taskRefs.has(c.id) || (c.task_id && taskRefs.has(c.task_id))) used.add(c.tag);
    }
  }
  return used;
}

export function ChatPanel({
  messages,
  streaming,
  onOpenTask,
  onNewChat,
}: {
  messages: ChatMessage[];
  streaming: boolean;
  onOpenTask: (taskId: string) => void;
  onNewChat: () => void;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  if (messages.length === 0) {
    return <EmptyState />;
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <button
          type="button"
          onClick={onNewChat}
          className="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs text-muted-foreground hover:bg-accent"
        >
          <Plus className="h-3 w-3" />
          New chat
        </button>
      </div>
      {messages.map((m, i) =>
        m.role === "user" ? (
          <UserBubble key={i} content={m.content} />
        ) : (
          <AssistantBubble
            key={i}
            message={m}
            streaming={streaming && i === messages.length - 1}
            onOpenTask={onOpenTask}
          />
        ),
      )}
      <div ref={bottomRef} />
    </div>
  );
}

function UserBubble({ content }: { content: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-primary px-3.5 py-2 text-[15px] text-primary-foreground">
        {content}
      </div>
    </div>
  );
}

function AssistantBubble({
  message,
  streaming,
  onOpenTask,
}: {
  message: ChatMessage;
  streaming: boolean;
  onOpenTask: (taskId: string) => void;
}) {
  const showTyping = message.pending && !message.content;
  // Show only sources the answer cited, plus every Drive result.
  const sources = useMemo(() => {
    const used = usedTags(message);
    return message.citations.filter((c) => c.type === "drive" || used.has(c.tag));
  }, [message]);

  return (
    <div className="max-w-[92%] space-y-2">
      {message.tools.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {message.tools.map((t, i) => (
            <ToolChip key={i} trace={t} />
          ))}
        </div>
      )}
      {showTyping ? (
        <div className="flex items-center gap-2 text-[15px] text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Thinking…
        </div>
      ) : (
        message.content && (
          <AssistantContent
            content={message.content}
            citations={message.citations}
            onOpenTask={onOpenTask}
          />
        )
      )}
      {streaming && message.content && (
        <span className="inline-block h-3 w-1.5 animate-pulse rounded-sm bg-muted-foreground align-middle" />
      )}
      {!message.pending && sources.length > 0 && (
        <div className="mt-1 space-y-1.5">
          <div className="px-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Sources
          </div>
          {sources.map((c) => (
            <SourceRow key={c.tag} cite={c} onOpenTask={onOpenTask} />
          ))}
        </div>
      )}
    </div>
  );
}

function ToolChip({ trace }: { trace: ChatToolTrace }) {
  const failed = trace.status === "failed";
  return (
    <span
      title={trace.result_summary}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs",
        failed
          ? "border-destructive/30 bg-destructive/10 text-destructive"
          : "border-border bg-muted text-muted-foreground",
      )}
    >
      <Wrench className="h-3 w-3" />
      <span className="font-medium">{trace.purpose || trace.name}</span>
      {failed ? <X className="h-3 w-3" /> : <Check className="h-3 w-3" />}
    </span>
  );
}

// "Alice <a@x.com>" → "Alice".
function displaySender(from: string): string {
  const m = from.match(/^"?([^"<]*?)"?\s*<([^>]+)>$/);
  const name = m ? m[1].trim() || m[2].trim() : from;
  return name.replace(/\s*\([^)]*\)\s*$/, "").trim() || name;
}

function metaLine(cite: ChatCitation): string {
  const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  return [
    cite.sender ? displaySender(cite.sender) : null,
    cite.ts ? fmtWhen(cite.ts) : null,
    cite.source && cite.source !== cite.type ? cap(cite.source) : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

// A source styled like a type-ahead result: clickable (task/drive/calendar) or
// expandable (input/note); inputs are both — expand to read, ↗ to open.
function SourceRow({
  cite,
  onOpenTask,
}: {
  cite: ChatCitation;
  onOpenTask: (taskId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const Icon = citationIcon(cite);
  const openTask = cite.task_id ?? (cite.type === "task" ? cite.id : null);
  const openUrl = !openTask && cite.url ? cite.url : null;
  const canOpen = Boolean(openTask || openUrl);
  const expandable = (cite.type === "input" || cite.type === "note") && Boolean(cite.snippet);

  const doOpen = () => {
    if (openTask) onOpenTask(openTask);
    else if (openUrl) window.open(openUrl, "_blank", "noopener,noreferrer");
  };
  const onRowClick = () => {
    if (expandable) setOpen((o) => !o);
    else if (canOpen) doOpen();
  };
  const meta = metaLine(cite);

  return (
    <div className="overflow-hidden rounded-xl border bg-card shadow-sm">
      <button
        type="button"
        disabled={!expandable && !canOpen}
        onClick={onRowClick}
        className={cn(
          "flex w-full items-center gap-2.5 px-3 py-2 text-left",
          expandable || canOpen
            ? "cursor-pointer hover:bg-accent hover:text-accent-foreground"
            : "cursor-default",
        )}
      >
        <span className="inline-flex h-4 w-6 shrink-0 items-center justify-center rounded bg-primary/15 text-[10px] font-semibold text-primary">
          {cite.tag}
        </span>
        <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{cite.title || "Untitled"}</span>
          {meta && <span className="mt-0.5 block truncate text-xs text-muted-foreground">{meta}</span>}
        </span>
        {expandable && canOpen && (
          <span
            role="button"
            tabIndex={-1}
            aria-label="Open source"
            onClick={(e) => {
              e.stopPropagation();
              doOpen();
            }}
            className="shrink-0 rounded p-1 text-muted-foreground hover:bg-muted"
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </span>
        )}
        {expandable && (
          <ChevronDown
            className={cn(
              "h-4 w-4 shrink-0 text-muted-foreground transition-transform",
              open && "rotate-180",
            )}
          />
        )}
      </button>
      {open && cite.snippet && (
        <div className="whitespace-pre-wrap border-t bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
          {cite.snippet}
        </div>
      )}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="py-16 text-center text-[15px] text-muted-foreground">
      <p className="font-medium text-foreground">Ask about your tasks, inbox, notes and calendar.</p>
      <p className="mt-2">Try “what’s due this week?” or “create a task to email Alice tomorrow”.</p>
    </div>
  );
}
