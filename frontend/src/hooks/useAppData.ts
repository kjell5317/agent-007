import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { RawInput, Task } from "@/lib/types";

const INPUTS_PAGE_SIZE = 20;
// Multi-device sync: refetch while the tab is visible. 20s is a compromise
// between "another device's edit shows up quickly" and "we don't hammer the
// API". When the tab is hidden we stop polling; when it becomes visible we
// fire one immediate refresh and resume the interval.
const POLL_INTERVAL_MS = 20_000;

export interface AppData {
  tasks: Task[];
  inputs: RawInput[];
  closedTasks: Task[];
  loading: boolean;
  refresh: () => Promise<void>;
  loadMoreInputs: () => Promise<void>;
  hasMoreInputs: boolean;
}

export function useAppData(): AppData {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [inputs, setInputs] = useState<RawInput[]>([]);
  const [closedTasks, setClosedTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [hasMoreInputs, setHasMoreInputs] = useState(false);
  // Held in a ref so refresh/loadMoreInputs keep stable identities across renders.
  const inputsLimitRef = useRef(INPUTS_PAGE_SIZE);

  const refresh = useCallback(async () => {
    const limit = inputsLimitRef.current;
    const [open, allInputs, closed] = await Promise.all([
      api.listTasks("open"),
      api.listInputs({ limit }),
      api.listTasks("closed"),
    ]);
    setTasks([...open]);
    setInputs(allInputs);
    setClosedTasks(closed);
    setHasMoreInputs(allInputs.length >= limit);
    setLoading(false);
  }, []);

  const loadMoreInputs = useCallback(async () => {
    const next = inputsLimitRef.current + INPUTS_PAGE_SIZE;
    const more = await api.listInputs({ limit: next });
    inputsLimitRef.current = next;
    setInputs(more);
    setHasMoreInputs(more.length >= next);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Multi-device sync. Strategy:
  //   1. Periodic poll while the document is visible.
  //   2. Immediate refresh on focus / visibilitychange-to-visible — gives a
  //      crisp "I unlocked my phone, show me the latest" feel.
  // We track in-flight requests with a ref so overlapping refreshes from
  // the timer + a focus event don't double-fire.
  useEffect(() => {
    let timer: number | null = null;
    let cancelled = false;
    let inFlight = false;

    const safeRefresh = async () => {
      if (inFlight || cancelled) return;
      inFlight = true;
      try {
        await refresh();
      } catch {
        // Swallow — periodic refresh failures shouldn't toast or surface.
      } finally {
        inFlight = false;
      }
    };

    const startTimer = () => {
      if (timer !== null) return;
      timer = window.setInterval(safeRefresh, POLL_INTERVAL_MS);
    };
    const stopTimer = () => {
      if (timer === null) return;
      window.clearInterval(timer);
      timer = null;
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        safeRefresh();
        startTimer();
      } else {
        stopTimer();
      }
    };
    const onFocus = () => {
      safeRefresh();
    };

    if (document.visibilityState === "visible") startTimer();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", onFocus);

    return () => {
      cancelled = true;
      stopTimer();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", onFocus);
    };
  }, [refresh]);

  return {
    tasks,
    inputs,
    closedTasks,
    loading,
    refresh,
    loadMoreInputs,
    hasMoreInputs,
  };
}
