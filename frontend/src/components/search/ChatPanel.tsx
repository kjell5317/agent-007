import { Check, Loader2, MessageSquare, Wrench, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { AssistantContent } from "@/components/search/AssistantContent";
import { fmtWhen } from "@/lib/dates";
import { cn } from "@/lib/utils";
import type { ChatMessage, ChatSummary, ChatToolTrace } from "@/lib/types";

export function ChatPanel({
  messages,
  streaming,
  onOpenTask,
  recent,
  onLoadChat,
}: {
  messages: ChatMessage[];
  streaming: boolean;
  onOpenTask: (taskId: string) => void;
  recent: ChatSummary[];
  onLoadChat: (id: string) => void;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  if (messages.length === 0) {
    return <EmptyState recent={recent} onLoadChat={onLoadChat} />;
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

function EmptyState({
  recent,
  onLoadChat,
}: {
  recent: ChatSummary[];
  onLoadChat: (id: string) => void;
}) {
  return (
    <div className="space-y-6">
      <div className="pt-10 text-center text-[15px] text-muted-foreground">
        <p className="font-medium text-foreground">
          Ask about your tasks, inbox, notes and calendar.
        </p>
        <p className="mt-2">Try “what’s due this week?” or “create a task to email Alice tomorrow”.</p>
      </div>
      {recent.length > 0 && (
        <div>
          <div className="mb-2 px-1 text-xs font-medium uppercase tracking-wider text-muted-foreground">
            Recent chats
          </div>
          <ul className="space-y-1.5">
            {recent.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => onLoadChat(c.id)}
                  className="flex w-full items-center gap-2.5 rounded-xl border bg-card px-3 py-2.5 text-left shadow-sm transition-colors hover:border-primary/40 hover:bg-accent hover:text-accent-foreground"
                >
                  <MessageSquare className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1 truncate text-sm font-medium">
                    {c.title || "Untitled chat"}
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {fmtWhen(c.updated_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
