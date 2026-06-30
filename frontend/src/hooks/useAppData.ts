import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { subscribeEvents } from "@/lib/events";
import { groupInputs } from "@/lib/inbox";
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

// Server task lists are ordered by the displayed task date
// (scheduled_date ?? due_date), then created_at. Live upserts re-sort to match
// so a pushed row lands where a refetch would have put it.
function upsertTask(list: Task[], task: Task): Task[] {
  const rest = list.filter((t) => t.id !== task.id);
  // The hook only holds *open* tasks; a non-open push means it left the list.
  if (task.status !== "open") return rest;
  return [...rest, task].sort(compareTasksBySchedule);
}

function upsertInput(list: RawInput[], input: RawInput): RawInput[] {
  const rest = list.filter((r) => r.id !== input.id);
  return [...rest, input].sort((a, b) => b.received_at.localeCompare(a.received_at));
}

export function useAppData(): AppData {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [inputs, setInputs] = useState<RawInput[]>([]);
  const [loading, setLoading] = useState(true);
  const [hasMoreInputs, setHasMoreInputs] = useState(false);
  // Held in a ref so refresh/loadMoreInputs keep stable identities across renders.
  const inputsLimitRef = useRef(INPUTS_PAGE_SIZE);

  const refresh = useCallback(async () => {
    const limit = inputsLimitRef.current;
    const [open, allInputs] = await Promise.all([
      api.listTasks("open"),
      api.listInputs({ limit }),
    ]);
    setTasks([...open]);
    setInputs(allInputs);
    setHasMoreInputs(groupInputs(allInputs).length >= limit);
    setLoading(false);
  }, []);

  const loadMoreInputs = useCallback(async () => {
    const next = inputsLimitRef.current + INPUTS_PAGE_SIZE;
    const more = await api.listInputs({ limit: next });
    inputsLimitRef.current = next;
    setInputs(more);
    setHasMoreInputs(groupInputs(more).length >= next);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

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
          setInputs((prev) => upsertInput(prev, event.data));
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
