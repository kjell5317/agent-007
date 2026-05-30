import { api } from "./api";
import { subscribeEvents } from "./events";
import type { RawInput } from "./types";

const TIMEOUT_MS = 120_000;

export interface PollHandle {
  cancel: () => void;
}

export interface PollCallbacks {
  onSuccess: () => void;
  onFailure: (message: string) => void;
  onTimeout: () => void;
}

type Trace =
  | { outcome?: string; manual_override?: { outcome?: string } }
  | null
  | undefined;

function terminal(input: Pick<RawInput, "task_id" | "agent_trace">):
  | "success"
  | "failure"
  | null {
  if (input.task_id) return "success";
  const trace = input.agent_trace as Trace;
  const failed =
    trace?.outcome === "task_creation_failed" ||
    trace?.manual_override?.outcome === "task_creation_failed";
  return failed ? "failure" : null;
}

/**
 * Resolve when the task-creation worker finishes a given raw_input.
 *
 * Works for both flows that enqueue on the task-creation queue:
 *   - POST /tasks               (fresh manual create)
 *   - POST /tasks/open/{id}     (manual override of an existing input)
 *
 * Driven by the shared SSE stream — the worker pushes the updated RawInput
 * when it's done, so there's no polling. One immediate `getInput` covers the
 * race where the worker finished between the POST returning and us
 * subscribing. Completion:
 *   - `task_id` set                          → success
 *   - `agent_trace(.manual_override).outcome` === "task_creation_failed"
 *                                             → failure
 *
 * Callers MUST invoke `cancel()` on unmount to drop the subscription.
 */
export function pollTaskCreation(
  rawInputId: string,
  callbacks: PollCallbacks,
): PollHandle {
  let done = false;
  let unsubscribe: (() => void) | null = null;
  let timeoutId: number | null = null;

  const finish = (run: () => void) => {
    if (done) return;
    done = true;
    if (timeoutId !== null) window.clearTimeout(timeoutId);
    if (unsubscribe) unsubscribe();
    run();
  };

  const evaluate = (input: Pick<RawInput, "task_id" | "agent_trace">) => {
    const state = terminal(input);
    if (state === "success") finish(callbacks.onSuccess);
    else if (state === "failure")
      finish(() => callbacks.onFailure("Task creation failed"));
  };

  timeoutId = window.setTimeout(() => finish(callbacks.onTimeout), TIMEOUT_MS);

  unsubscribe = subscribeEvents((event) => {
    if (event.type === "input" && event.data.id === rawInputId) {
      evaluate(event.data);
    }
  });

  // Catch the case where the worker already finished before we subscribed.
  api
    .getInput(rawInputId)
    .then(evaluate)
    .catch(() => {
      // Ignore — the SSE stream is the primary signal; a failed one-shot
      // check just means we wait for the push (or time out).
    });

  return { cancel: () => finish(() => {}) };
}
