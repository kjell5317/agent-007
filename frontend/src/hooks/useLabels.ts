import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Label } from "@/lib/types";

let cache: Label[] | null = null;
let inflight: Promise<Label[]> | null = null;

// Labels are config — they rarely change. Fetch once, share across the app.
export function useLabels(): Label[] {
  const [labels, setLabels] = useState<Label[]>(cache ?? []);

  useEffect(() => {
    if (cache) return;
    if (!inflight) {
      inflight = api.listLabels().then((rows) => {
        cache = rows;
        return rows;
      });
    }
    inflight.then(setLabels).catch(() => setLabels([]));
  }, []);

  return labels;
}
