import { Check, ChevronDown, Loader2, MessageSquare, Wrench, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AssistantContent } from "@/components/search/AssistantContent";
import { Modal } from "@/components/ui/modal";
import { fmtWhen } from "@/lib/dates";
import { cn } from "@/lib/utils";
import type { ChatCitation, ChatMessage, ChatSummary, ChatToolTrace } from "@/lib/types";

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
  // A citation with no navigable target (note / url-less input) opens here.
  const [preview, setPreview] = useState<ChatCitation | null>(null);
  // Stick to the bottom as content streams in, unless the user scrolled up to
  // read — then leave their position alone until the next turn.
  const stick = useRef(true);
  const prevLen = useRef(messages.length);

  useEffect(() => {
    const onScroll = () => {
      const el = document.documentElement;
      stick.current = el.scrollHeight - (window.scrollY + window.innerHeight) < 160;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    const grew = messages.length !== prevLen.current;
    prevLen.current = messages.length;
    if (grew) stick.current = true; // a new turn always jumps to the bottom
    if (!stick.current) return;
    const id = requestAnimationFrame(() =>
      window.scrollTo({ top: document.documentElement.scrollHeight }),
    );
    return () => cancelAnimationFrame(id);
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
            onShowContent={setPreview}
          />
        ),
      )}
      <CitationModal cite={preview} onClose={() => setPreview(null)} onOpenTask={onOpenTask} />
    </div>
  );
}

function CitationModal({
  cite,
  onClose,
  onOpenTask,
}: {
  cite: ChatCitation | null;
  onClose: () => void;
  onOpenTask: (taskId: string) => void;
}) {
  if (!cite) return null;
  const openTask = cite.task_id ?? (cite.type === "task" ? cite.id : null);
  return (
    <Modal open onClose={onClose} title={cite.title || "Source"}>
      <div className="space-y-3">
        <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">
          {cite.snippet || "No preview available."}
        </p>
        {(openTask || cite.url) && (
          <button
            type="button"
            onClick={() => {
              if (openTask) onOpenTask(openTask);
              else if (cite.url) window.open(cite.url, "_blank", "noopener,noreferrer");
              onClose();
            }}
            className="text-sm font-medium text-primary hover:underline"
          >
            {openTask ? "Open task" : "Open source"}
          </button>
        )}
      </div>
    </Modal>
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
  onShowContent,
}: {
  message: ChatMessage;
  streaming: boolean;
  onOpenTask: (taskId: string) => void;
  onShowContent: (cite: ChatCitation) => void;
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
            caret={streaming}
            onOpenTask={onOpenTask}
            onShowContent={onShowContent}
          />
        )
      )}
    </div>
  );
}

interface PanelPos {
  left: number;
  width: number;
  top?: number;
  bottom?: number;
  maxHeight: number;
}

function ToolChip({ trace }: { trace: ChatToolTrace }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<PanelPos | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const failed = trace.status === "failed";
  const params =
    trace.params && Object.keys(trace.params).length > 0 ? trace.params : null;
  const result = trace.result?.trim() || null;
  const hasDetail = Boolean(params || result);

  // Anchor the panel to the chip in viewport coordinates so it never gets
  // clipped by a scrolling ancestor: flip above when there's no room below,
  // and clamp within the viewport horizontally and in height.
  const place = useCallback(() => {
    const btn = btnRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const margin = 12;
    const gap = 6;
    const maxH = 320;
    const width = Math.min(320, window.innerWidth - margin * 2);
    const left = Math.max(margin, Math.min(r.left, window.innerWidth - width - margin));
    const spaceBelow = window.innerHeight - r.bottom;
    const openUp = spaceBelow < 240 && r.top > spaceBelow;
    const next: PanelPos = openUp
      ? {
          left,
          width,
          bottom: window.innerHeight - r.top + gap,
          maxHeight: Math.min(maxH, r.top - gap - margin),
        }
      : {
          left,
          width,
          top: r.bottom + gap,
          maxHeight: Math.min(maxH, window.innerHeight - r.bottom - gap - margin),
        };
    setPos(next);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || panelRef.current?.contains(t)) return;
      setOpen(false);
    };
    const reposition = () => place();
    document.addEventListener("mousedown", onDown);
    // Capture-phase scroll catches ancestor scroll containers too.
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [open, place]);

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        disabled={!hasDetail}
        onClick={() => {
          if (!hasDetail) return;
          setOpen((v) => {
            if (!v) place();
            return !v;
          });
        }}
        title={trace.result_summary}
        className={cn(
          "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs transition-colors",
          failed
            ? "border-destructive/30 bg-destructive/10 text-destructive"
            : "border-border bg-muted text-muted-foreground",
          hasDetail && "cursor-pointer hover:text-foreground",
        )}
      >
        <Wrench className="h-3 w-3" />
        <span className="max-w-[16rem] truncate font-medium">{trace.purpose || trace.name}</span>
        {failed ? <X className="h-3 w-3" /> : <Check className="h-3 w-3" />}
        {hasDetail && (
          <ChevronDown
            className={cn("h-3 w-3 transition-transform", open && "rotate-180")}
          />
        )}
      </button>
      {open &&
        hasDetail &&
        pos &&
        createPortal(
          <div
            ref={panelRef}
            style={{
              position: "fixed",
              left: pos.left,
              top: pos.top,
              bottom: pos.bottom,
              width: pos.width,
              maxHeight: pos.maxHeight,
            }}
            className="z-50 space-y-2.5 overflow-y-auto rounded-xl border bg-card p-3 text-xs shadow-lg"
          >
            <div className="font-mono text-[11px] text-muted-foreground">{trace.name}</div>
            {params && <ToolDetailSection title="Parameters" body={JSON.stringify(params, null, 2)} />}
            {result && <ToolDetailSection title="Result" body={result} />}
          </div>,
          document.body,
        )}
    </>
  );
}

function ToolDetailSection({ title, body }: { title: string; body: string }) {
  return (
    <div className="space-y-1">
      <div className="font-medium uppercase tracking-wider text-muted-foreground">{title}</div>
      <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 font-mono text-[11px] text-foreground">
        {body}
      </pre>
    </div>
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
