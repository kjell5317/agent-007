import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { pollTaskCreation, type PollHandle } from "@/lib/pollTask";

interface PromoteOpts {
  title?: string;
  contextInputIds?: string[];
}

// Shared inbox actions for both single cards and thread groups. Promotion is
// async on the backend (queue + LLM extract), so we track the in-flight poll
// and cancel it if the component unmounts mid-flight.
export function useInboxActions(onChanged: () => Promise<void> | void) {
  const [busy, setBusy] = useState(false);
  const activePolls = useRef<Set<PollHandle>>(new Set());

  useEffect(
    () => () => {
      activePolls.current.forEach((handle) => handle.cancel());
      activePolls.current.clear();
    },
    [],
  );

  const runTaskAction = useCallback(
    async (
      taskId: string,
      call: (id: string) => Promise<unknown>,
      successMsg: string,
    ) => {
      setBusy(true);
      try {
        await call(taskId);
        toast.success(successMsg);
        await onChanged();
      } catch (e) {
        toast.error((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [onChanged],
  );

  const promote = useCallback(
    async (anchorId: string, opts?: PromoteOpts) => {
      setBusy(true);
      const toastId = toast.loading("Creating task…", { duration: Infinity });
      let handle: PollHandle | null = null;
      const finish = (run: () => void) => {
        toast.dismiss(toastId);
        run();
        if (handle) activePolls.current.delete(handle);
        setBusy(false);
      };
      try {
        const { raw_input_id } = await api.promoteInput(anchorId, opts);
        handle = pollTaskCreation(raw_input_id, {
          onSuccess: () =>
            finish(() => {
              toast.success("Task added");
              void onChanged();
            }),
          onFailure: (message) => finish(() => toast.error(message)),
          onTimeout: () =>
            finish(() => toast.error("Task is taking longer than expected")),
        });
        activePolls.current.add(handle);
      } catch (err) {
        toast.dismiss(toastId);
        toast.error((err as Error).message);
        setBusy(false);
      }
    },
    [onChanged],
  );

  const reopenTask = useCallback(
    async (taskId: string) => {
      setBusy(true);
      const toastId = toast.loading("Re-opening task…", { duration: Infinity });
      let handle: PollHandle | null = null;
      const finish = (run: () => void) => {
        toast.dismiss(toastId);
        run();
        if (handle) activePolls.current.delete(handle);
        setBusy(false);
      };
      try {
        const { raw_input_id } = await api.reopenTask(taskId);
        handle = pollTaskCreation(raw_input_id, {
          onSuccess: () =>
            finish(() => {
              toast.success("Task re-opened");
              void onChanged();
            }),
          onFailure: (message) => finish(() => toast.error(message)),
          onTimeout: () =>
            finish(() => toast.error("Task is taking longer than expected")),
        });
        activePolls.current.add(handle);
      } catch (err) {
        toast.dismiss(toastId);
        toast.error((err as Error).message);
        setBusy(false);
      }
    },
    [onChanged],
  );

  return { busy, runTaskAction, promote, reopenTask };
}
