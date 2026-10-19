import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { subscribeEvents } from "@/lib/events";
import { inputGroupKey } from "@/lib/inbox";
import { compareTasksBySchedule } from "@/lib/tasks";
import type { RawInput, Task } from "@/lib/types";

// Pagination counts inbox *groups* (threads / follow-ups), not raw rows — the
// backend returns every member of each included group (see `list_grouped`).
const INPUTS_PAGE_SIZE = 20;

export interface AppData {
  tasks: Task[];
  inputs: RawInput[];
  loading: boolean;
  refresh: () => Promise<void>;
  loadMoreInputs: () => Promise<void>;
  hasMoreInputs: boolean;
}

// Server task lists are ordered by scheduled_date first, then by a deterministic
// fallback for legacy/null scheduled rows. Live upserts re-sort to match so a
// pushed row lands where a refetch would have put it.
function upsertTask(list: Task[], task: Task): Task[] {
  const rest = list.filter((t) => t.id !== task.id);
  // The hook only holds *open* tasks; a non-open push means it left the list.
  if (task.status !== "open" || task.is_container) return rest;
  return [...rest, task].sort(compareTasksBySchedule);
}

function upsertInput(list: RawInput[], input: RawInput): RawInput[] {
  const rest = list.filter((r) => r.id !== input.id);
  return [...rest, input].sort((a, b) => b.received_at.localeCompare(a.received_at));
}

export function useAppData(inputSource = ""): AppData {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [inputs, setInputs] = useState<RawInput[]>([]);
  const [loading, setLoading] = useState(true);
  const [hasMoreInputs, setHasMoreInputs] = useState(false);
  // Held in refs so refresh/loadMoreInputs keep stable identities across renders.
  const inputsLimitRef = useRef(INPUTS_PAGE_SIZE);
  const inputSourceRef = useRef(inputSource);

  // Filtering by source happens server-side so every page holds `limit` groups
  // of that source. Returns false when the filter changed mid-flight.
  const fetchInputs = useCallback(async (limit: number) => {
    const source = inputSourceRef.current;
    const rows = await api.listInputs({ limit, source });
    if (source !== inputSourceRef.current) return false;
    setInputs(rows);
    // Count groups the way the backend's LIMIT does; groupInputs also folds
    // kotx runs into their tasks, which would undercount and end paging early.
    setHasMoreInputs(new Set(rows.map(inputGroupKey)).size >= limit);
    return true;
  }, []);

  const refresh = useCallback(async () => {
    const [open] = await Promise.all([
      api.listTasks("open"),
      fetchInputs(inputsLimitRef.current),
    ]);
    setTasks([...open]);
    setLoading(false);
  }, [fetchInputs]);

  const loadMoreInputs = useCallback(async () => {
    const next = inputsLimitRef.current + INPUTS_PAGE_SIZE;
    if (await fetchInputs(next)) inputsLimitRef.current = next;
  }, [fetchInputs]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (inputSourceRef.current === inputSource) return;
    inputSourceRef.current = inputSource;
    inputsLimitRef.current = INPUTS_PAGE_SIZE;
    fetchInputs(INPUTS_PAGE_SIZE).catch(() => {});
  }, [fetchInputs, inputSource]);

  // Live updates over the shared SSE stream replace periodic polling: the
  // backend pushes the full Task / RawInput on every mutation and we patch
  // state in place. EventSource reconnects on its own after a drop; the
  // focus/visibility refetch below closes any gap of events missed while
  // disconnected (and gives the "I just unlocked my phone" instant refresh).
  useEffect(() => {
    const unsubscribe = subscribeEvents((event) => {
      switch (event.type) {
        case "task":
          setTasks((prev) => upsertTask(prev, event.data));
          break;
        case "task_removed":
          setTasks((prev) => prev.filter((t) => t.id !== event.id));
          break;
        case "input":
          if (inputSourceRef.current && event.data.source !== inputSourceRef.current) break;
          setInputs((prev) => upsertInput(prev, event.data));
          break;
        case "kotx":
          // A kotx transition changes a linked task as well as the run, but
          // the per-task push can be skipped when the transition errors after
          // committing (e.g. reopen's schedule_task throws). Resync the task
          // list on the nudge — like useRuns does for runs — so a dropped task
          // self-heals instead of staying gone until a focus/visibility refetch.
          api
            .listTasks("open")
            .then((open) => setTasks([...open]))
            .catch(() => {});
          break;
      }
    });
    return unsubscribe;
  }, []);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;

    const safeRefresh = async () => {
      if (inFlight || cancelled) return;
      inFlight = true;
      try {
        await refresh();
      } catch {
        // Swallow — background refresh failures shouldn't toast or surface.
      } finally {
        inFlight = false;
      }
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
  }, [refresh]);

  return {
    tasks,
    inputs,
    loading,
    refresh,
    loadMoreInputs,
    hasMoreInputs,
  };
}
