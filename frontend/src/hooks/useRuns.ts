import { useCallback, useEffect, useRef, useState } from "react";
import { subscribeEvents } from "@/lib/events";
import { kotx, type KotxTask } from "@/lib/kotx";

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

  // Refresh once when the owning view becomes active (or its scope changes).
  useEffect(() => {
    if (!active) return;
    backgroundRefresh();
  }, [active, backgroundRefresh, scope]);

  // Live updates ride the shared SSE stream instead of an interval: the
  // backend publishes a `kotx` nudge whenever the kotx webhook (or the
  // reconciliation poll) lands a transition. Events missed while disconnected
  // are reconciled by the focus/visibility refetch above; kotx mutations from
  // this client still call `refresh` directly through onKotxChanged.
  useEffect(() => {
    if (!preload && !active) return;
    return subscribeEvents((event) => {
      if (event.type === "kotx") backgroundRefresh();
    });
  }, [active, backgroundRefresh, preload]);

  return { tasks, loading, error, scope, refresh };
}
