import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Label } from "@/lib/types";

let cache: Label[] | null = null;
let inflight: Promise<Label[]> | null = null;
const subscribers = new Set<(labels: Label[]) => void>();

// Labels change rarely (they're mirrored from the calendar). Fetch once, share
// across the app, and let the settings page push edits into every consumer.
export function useLabels(): Label[] {
  const [labels, setLabels] = useState<Label[]>(cache ?? []);

  useEffect(() => {
    subscribers.add(setLabels);
    if (cache) {
      setLabels(cache);
    } else {
      if (!inflight) {
        inflight = api.listLabels().then((rows) => {
          cache = rows;
          return rows;
        });
      }
      inflight.then(setLabels).catch(() => setLabels([]));
    }
    return () => {
      subscribers.delete(setLabels);
    };
  }, []);

  return labels;
}

// Called by the labels settings page after a sync or an edit, so task chips and
// pickers pick up the new descriptions without a reload.
export function primeLabels(rows: Label[]): void {
  cache = rows;
  inflight = Promise.resolve(rows);
  for (const notify of subscribers) notify(rows);
}
