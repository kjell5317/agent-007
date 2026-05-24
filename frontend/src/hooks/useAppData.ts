import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { RawInput, Task } from "@/lib/types";

export interface AppData {
  tasks: Task[];
  inputs: RawInput[];
  closedTasks: Task[];
  loading: boolean;
  refresh: () => Promise<void>;
}

export function useAppData(): AppData {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [inputs, setInputs] = useState<RawInput[]>([]);
  const [closedTasks, setClosedTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    const [open, dup, allInputs, closed] = await Promise.all([
      api.listTasks("open"),
      api.listTasks("duplicate"),
      api.listInputs({ limit: 200 }),
      api.listTasks("closed"),
    ]);
    setTasks([...open, ...dup]);
    setInputs(allInputs);
    setClosedTasks(closed);
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { tasks, inputs, closedTasks, loading, refresh };
}
