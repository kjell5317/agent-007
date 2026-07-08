import {
  CalendarDays,
  Check,
  FileText,
  HardDrive,
  Inbox,
  ListTodo,
  Loader2,
  Wrench,
  X,
} from "lucide-react";
import type { ComponentType } from "react";
import { useEffect, useRef } from "react";
import { AssistantContent } from "@/components/search/AssistantContent";
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

export function ChatPanel({
  messages,
  streaming,
  onOpenTask,
}: {
  messages: ChatMessage[];
  streaming: boolean;
  onOpenTask: (taskId: string) => void;
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
      <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-primary px-3.5 py-2 text-sm text-primary-foreground">
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
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
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
      {!message.pending && message.citations.length > 0 && (
        <Sources citations={message.citations} onOpenTask={onOpenTask} />
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

function Sources({
  citations,
  onOpenTask,
}: {
  citations: ChatCitation[];
  onOpenTask: (taskId: string) => void;
}) {
  return (
    <div className="mt-1 space-y-1 rounded-xl border bg-card/50 p-2">
      <div className="px-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        Sources
      </div>
      {citations.map((c) => (
        <CitationRow key={c.tag} cite={c} onOpenTask={onOpenTask} />
      ))}
    </div>
  );
}

function CitationRow({
  cite,
  onOpenTask,
}: {
  cite: ChatCitation;
  onOpenTask: (taskId: string) => void;
}) {
  const Icon = citationIcon(cite);
  const openTask = cite.task_id ?? (cite.type === "task" ? cite.id : null);
  const openUrl = !openTask && cite.url ? cite.url : null;
  const clickable = Boolean(openTask || openUrl);
  const activate = () => {
    if (openTask) onOpenTask(openTask);
    else if (openUrl) window.open(openUrl, "_blank", "noopener,noreferrer");
  };
  return (
    <button
      type="button"
      disabled={!clickable}
      onClick={clickable ? activate : undefined}
      className={cn(
        "flex w-full items-center gap-2 rounded-lg px-1.5 py-1 text-left text-xs",
        clickable ? "cursor-pointer hover:bg-accent" : "cursor-default",
      )}
    >
      <span className="inline-flex h-4 w-6 shrink-0 items-center justify-center rounded bg-primary/15 text-[10px] font-semibold text-primary">
        {cite.tag}
      </span>
      <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate">{cite.title || "Untitled"}</span>
    </button>
  );
}

function EmptyState() {
  return (
    <div className="py-16 text-center text-sm text-muted-foreground">
      <p className="font-medium text-foreground">Ask about your tasks, inbox, notes and calendar.</p>
      <p className="mt-2">Try “what’s due this week?” or “create a task to email Alice tomorrow”.</p>
    </div>
  );
}
