import { api } from "./api";

const POLL_INTERVAL_MS = 1000;
const POLL_TIMEOUT_MS = 120_000;

export interface PollHandle {
  cancel: () => void;
}

export interface PollCallbacks {
  onSuccess: () => void;
  onFailure: (message: string) => void;
  onTimeout: () => void;
}

/**
 * Poll a raw_input until the task-creation worker is done.
 *
 * Works for both flows that enqueue on the task-creation queue:
 *   - POST /tasks               (fresh manual create)
 *   - POST /tasks/open/{id}     (manual override of an existing input)
 *
 * Completion is detected by:
 *   - `task_id` becomes non-null              → success
 *   - `agent_trace.outcome` === "task_creation_failed"
 *     OR `agent_trace.manual_override.outcome` === "task_creation_failed"
 *                                             → failure
 *
 * Callers MUST invoke `cancel()` on unmount, otherwise the in-flight
 * setTimeout keeps the closure alive past the component's lifecycle.
 */
export function pollTaskCreation(
  rawInputId: string,
  callbacks: PollCallbacks,
): PollHandle {
  let cancelled = false;
  let timeoutId: number | null = null;
  const startedAt = Date.now();

  const tick = async () => {
    if (cancelled) return;
    try {
      const input = await api.getInput(rawInputId);
      if (cancelled) return;

      if (input.task_id) {
        callbacks.onSuccess();
        return;
      }

      const trace = input.agent_trace as
        | { outcome?: string; manual_override?: { outcome?: string } }
        | null
        | undefined;
      const failed =
        trace?.outcome === "task_creation_failed" ||
        trace?.manual_override?.outcome === "task_creation_failed";
      if (failed) {
        callbacks.onFailure("Task creation failed");
        return;
      }

      if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
        callbacks.onTimeout();
        return;
      }

      timeoutId = window.setTimeout(() => {
        timeoutId = null;
        void tick();
      }, POLL_INTERVAL_MS);
    } catch (err) {
      if (cancelled) return;
      callbacks.onFailure((err as Error).message);
    }
  };

  void tick();

  return {
    cancel: () => {
      cancelled = true;
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
        timeoutId = null;
      }
    },
  };
}
