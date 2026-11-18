import { useEffect } from "react";

export function useOnForeground(callback: () => unknown, enabled = true): void {
  useEffect(() => {
    if (!enabled) return;
    const run = () => {
      callback();
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") run();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", run);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", run);
    };
  }, [callback, enabled]);
}
