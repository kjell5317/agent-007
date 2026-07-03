import { useCallback, useEffect, useRef, useState } from "react";
import { kotx, type KotxTask } from "@/lib/kotx";

const POLL_MS = 5000;

export interface RunsData {
  tasks: KotxTask[];
  loading: boolean;
  // Set when the proxy is unreachable / unconfigured (503) or the first load
  // fails. Cleared on the next successful refresh.
  error: string | null;
  scope: "active" | "all";
  refresh: () => Promise<void>;
}

export function useRuns(
  active: boolean,
  scope: "active" | "all" = "active",
  preload = true,
): RunsData {
  const [tasks, setTasks] = useState<KotxTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const scopeRef = useRef(scope);
  const backgroundRefreshScopesRef = useRef(new Set<"active" | "all">());
  scopeRef.current = scope;

  const refresh = useCallback(async () => {
    // Capture the scope this fetch is for: a scope switch mid-flight must not
    // let the old scope's result land in (and clear the skeleton for) the new.
    const requested = scopeRef.current;
    try {
      const t = await kotx.listTasks(requested);
      if (scopeRef.current !== requested) return;
      setTasks(t);
      setError(null);
    } catch (e) {
      if (scopeRef.current !== requested) return;
      setError((e as Error).message);
    } finally {
      if (scopeRef.current === requested) setLoading(false);
    }
  }, []);

  const backgroundRefresh = useCallback(async () => {
    const requested = scopeRef.current;
    if (backgroundRefreshScopesRef.current.has(requested)) return;

    backgroundRefreshScopesRef.current.add(requested);
    try {
      await refresh();
    } catch {
      // `refresh` owns visible error state; background triggers should not throw.
    } finally {
      backgroundRefreshScopesRef.current.delete(requested);
    }
  }, [refresh]);

  // Fetch once on mount even when the tab is closed, so the Runs badge count
  // is populated on page load rather than only after the tab is first opened.
  useEffect(() => {
    if (!preload) return;
    backgroundRefresh();
  }, [backgroundRefresh, preload]);

  // Refresh whenever the page returns to the foreground. Preloaded hooks keep
  // badges current even while their view is closed; non-preloaded hooks only
  // listen while their view is active.
  useEffect(() => {
    if (!preload && !active) return;

    let cancelled = false;
    const safeRefresh = () => {
      if (!cancelled) backgroundRefresh();
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") safeRefresh();
    };

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", safeRefresh);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", safeRefresh);
    };
  }, [active, backgroundRefresh, preload]);

  // Refresh once when the owning view becomes active. Only active-scope hooks
  // use the short interval: the all-scope list can be much larger, and kotx
  // mutations still call `refresh` directly through onKotxChanged.
  useEffect(() => {
    if (!active) return;
    backgroundRefresh();
    if (scope !== "active") return;

    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer == null && document.visibilityState === "visible") {
        timer = setInterval(backgroundRefresh, POLL_MS);
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
  }, [active, backgroundRefresh, scope]);

  return { tasks, loading, error, scope, refresh };
}
