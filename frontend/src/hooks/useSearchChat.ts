import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type {
  ChatCitation,
  ChatMessage,
  ChatSummary,
  ChatToolTrace,
} from "@/lib/types";

const HISTORY = 5;
const RECENT = 5;

interface SearchChat {
  messages: ChatMessage[];
  streaming: boolean;
  recent: ChatSummary[];
  send: (text: string) => void;
  newChat: () => void;
  loadChat: (id: string) => void;
}

function deriveTitle(messages: ChatMessage[]): string {
  const firstUser = messages.find((m) => m.role === "user");
  return (firstUser?.content ?? "New chat").slice(0, 120);
}

/**
 * Holds the chat conversation and streams `/search/chat`. Conversations are
 * persisted server-side: the recent list is fetched for the empty-chat view,
 * a completed turn is saved (create then update), and `loadChat` reopens one.
 * Streamed pre-tool preamble ("thinking") is dropped — the content resets when
 * a tool runs, so only the final post-tool answer is shown.
 */
export function useSearchChat(): SearchChat {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [recent, setRecent] = useState<ChatSummary[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const conversationIdRef = useRef<string | null>(null);
  const messagesRef = useRef<ChatMessage[]>(messages);
  const prevStreaming = useRef(false);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  const patchLast = useCallback((patch: (m: ChatMessage) => ChatMessage) => {
    setMessages((prev) => {
      if (prev.length === 0) return prev;
      const next = prev.slice();
      next[next.length - 1] = patch(next[next.length - 1]);
      return next;
    });
  }, []);

  const refreshRecent = useCallback(async () => {
    try {
      setRecent(await api.listChats(RECENT));
    } catch {
      // recent list is best-effort
    }
  }, []);

  useEffect(() => {
    void refreshRecent();
  }, [refreshRecent]);

  const persist = useCallback(async () => {
    const msgs = messagesRef.current;
    if (msgs.length === 0) return;
    const body = { title: deriveTitle(msgs), messages: msgs };
    try {
      if (conversationIdRef.current) {
        await api.updateChat(conversationIdRef.current, body);
      } else {
        const created = await api.createChat(body);
        conversationIdRef.current = created.id;
      }
      void refreshRecent();
    } catch {
      // persistence is best-effort; the conversation stays in memory
    }
  }, [refreshRecent]);

  // Persist once a turn finishes streaming (not on plain message edits / loads).
  useEffect(() => {
    const justFinished = prevStreaming.current && !streaming;
    prevStreaming.current = streaming;
    if (justFinished) void persist();
  }, [streaming, persist]);

  const newChat = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    conversationIdRef.current = null;
    setStreaming(false);
    setMessages([]);
  }, []);

  const loadChat = useCallback(async (id: string) => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    try {
      const conv = await api.getChat(id);
      conversationIdRef.current = conv.id;
      setMessages(conv.messages.map((m) => ({ ...m, pending: false })));
    } catch {
      // ignore a failed load
    }
  }, []);

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || streaming) return;

      const user: ChatMessage = {
        role: "user",
        content: trimmed,
        citations: [],
        tools: [],
        pending: false,
      };
      const assistant: ChatMessage = {
        role: "assistant",
        content: "",
        citations: [],
        tools: [],
        pending: true,
      };
      const history = [...messages, user];
      setMessages([...history, assistant]);
      setStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;
      const wire = history.slice(-HISTORY).map((m) => ({ role: m.role, content: m.content }));

      api
        .chatStream(wire, controller.signal, {
          onCitations: (items: ChatCitation[]) =>
            patchLast((m) => ({ ...m, citations: dedupeTags([...m.citations, ...items]) })),
          onToken: (t: string) =>
            patchLast((m) => ({ ...m, content: m.content + t, pending: false })),
          onTool: (trace: ChatToolTrace) =>
            // Drop any streamed preamble ("thinking") — keep only the tool chips
            // and let the post-tool answer stream fresh.
            patchLast((m) => ({ ...m, tools: [...m.tools, trace], content: "", pending: true })),
          onError: (msg: string) =>
            patchLast((m) => ({
              ...m,
              content: m.content + (m.content ? "\n\n" : "") + `⚠️ ${msg}`,
              pending: false,
            })),
        })
        .catch((err) => {
          if (controller.signal.aborted) return;
          patchLast((m) => ({
            ...m,
            content: m.content || `⚠️ ${(err as Error).message}`,
            pending: false,
          }));
        })
        .finally(() => {
          if (abortRef.current === controller) abortRef.current = null;
          patchLast((m) => ({ ...m, pending: false }));
          setStreaming(false);
        });
    },
    [messages, patchLast, streaming],
  );

  return { messages, streaming, recent, send, newChat, loadChat };
}

function dedupeTags(items: ChatCitation[]): ChatCitation[] {
  const seen = new Set<string>();
  const out: ChatCitation[] = [];
  for (const it of items) {
    if (seen.has(it.tag)) continue;
    seen.add(it.tag);
    out.push(it);
  }
  return out;
}
