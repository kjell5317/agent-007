import { useCallback, useEffect, useRef, useState } from "react";
import { kotx, type KotxContainer, type KotxTask } from "@/lib/kotx";

const POLL_MS = 5000;

export interface RunsData {
  tasks: KotxTask[];
  containers: KotxContainer[];
  loading: boolean;
  // Set when the proxy is unreachable / unconfigured (503) or the first load
  // fails. Cleared on the next successful refresh.
  error: string | null;
  scope: "active" | "all";
  setScope: (s: "active" | "all") => void;
  refresh: () => Promise<void>;
}

export function useRuns(active: boolean): RunsData {
  const [tasks, setTasks] = useState<KotxTask[]>([]);
  const [containers, setContainers] = useState<KotxContainer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scope, setScope] = useState<"active" | "all">("active");
  const scopeRef = useRef(scope);
  scopeRef.current = scope;

  const refresh = useCallback(async () => {
    try {
      const [t, c] = await Promise.all([
        kotx.listTasks(scopeRef.current),
        kotx.listContainers(),
      ]);
      setTasks(t);
      setContainers(c);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  // Fetch once on mount even when the tab is closed, so the Runs badge count
  // is populated on page load rather than only after the tab is first opened.
  useEffect(() => {
    refresh();
  }, [refresh]);

  // Only poll while the tab is the visible one and the document has focus —
  // runs change state over seconds (queued → running → awaiting_approval),
  // so a short interval keeps the list and container view live.
  useEffect(() => {
    if (!active) return;
    refresh();
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer == null && document.visibilityState === "visible") {
        timer = setInterval(refresh, POLL_MS);
      }
    };
    const stop = () => {
      if (timer != null) {
        clearInterval(timer);
        timer = null;
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        refresh();
        start();
      } else {
        stop();
      }
    };
    start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [active, refresh, scope]);

  return { tasks, containers, loading, error, scope, setScope, refresh };
}
