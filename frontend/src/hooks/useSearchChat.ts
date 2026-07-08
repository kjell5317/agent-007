import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { ChatCitation, ChatMessage, ChatToolTrace } from "@/lib/types";

const HISTORY = 5;
const STORAGE_KEY = "search-chat-history";
const STORE_MAX = 40;

interface SearchChat {
  messages: ChatMessage[];
  streaming: boolean;
  send: (text: string) => void;
  reset: () => void;
}

function loadStored(): ChatMessage[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Clear any spinner left over from a message that was mid-stream on unload.
    return parsed.map((m: ChatMessage) => ({ ...m, pending: false }));
  } catch {
    return [];
  }
}

/**
 * Holds the chat conversation and streams `/search/chat`. Each `send` posts the
 * last few turns, appends a pending assistant message, and folds the SSE events
 * (citations / tokens / tool traces) into it as they arrive. Opening a new send
 * while one is in flight is ignored; `reset` aborts and clears.
 */
export function useSearchChat(): SearchChat {
  const [messages, setMessages] = useState<ChatMessage[]>(loadStored);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Persist the conversation so reopening the view shows the last chat. Skip
  // while streaming — the transient token updates would thrash localStorage;
  // the final state persists once streaming settles.
  useEffect(() => {
    if (streaming) return;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(messages.slice(-STORE_MAX)));
    } catch {
      // storage full / unavailable — history just won't persist this session
    }
  }, [messages, streaming]);

  // Mutate the trailing (assistant) message in place.
  const patchLast = useCallback((patch: (m: ChatMessage) => ChatMessage) => {
    setMessages((prev) => {
      if (prev.length === 0) return prev;
      const next = prev.slice();
      next[next.length - 1] = patch(next[next.length - 1]);
      return next;
    });
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setMessages([]);
    setStreaming(false);
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      // ignore
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
            patchLast((m) => ({ ...m, tools: [...m.tools, trace] })),
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

  return { messages, streaming, send, reset };
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
