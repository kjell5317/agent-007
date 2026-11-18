import { toast as sonner } from "sonner";

// While a task runs the app stays quiet: only errors get through.
let quiet = false;
let quietId = 0;

export function setQuietToasts(next: boolean): void {
  quiet = next;
}

export const toast = {
  success: (...args: Parameters<typeof sonner.success>) =>
    quiet ? undefined : sonner.success(...args),
  // A swallowed loading toast still needs an id of its own: callers dismiss by
  // id, and dismiss() without one clears every toast, errors included.
  loading: (...args: Parameters<typeof sonner.loading>) =>
    quiet ? `quiet-${++quietId}` : sonner.loading(...args),
  error: sonner.error,
  dismiss: sonner.dismiss,
};
