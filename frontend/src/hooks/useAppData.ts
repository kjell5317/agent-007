import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { RawInput, Task } from "@/lib/types";

const INPUTS_PAGE_SIZE = 20;

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
