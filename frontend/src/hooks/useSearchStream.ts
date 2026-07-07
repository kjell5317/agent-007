import { useEffect, useState } from "react";
import type { SearchHit } from "@/lib/types";

const DEBOUNCE_MS = 150;

interface SearchStream {
  hits: SearchHit[];
  loading: boolean;
}

/**
 * Streams `/search/stream` results for a query. Each keystroke (debounced)
 * opens a fresh `EventSource`; rows arrive as `hit` events and are appended so
 * the list fills incrementally, and a `done` event closes the stream. Opening
 * a new query — or unmounting — aborts the in-flight one, so results never
 * interleave across queries.
 */
export function useSearchStream(query: string): SearchStream {
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let source: EventSource | null = null;
    const timer = window.setTimeout(() => {
      setHits([]);
      setLoading(true);
      source = new EventSource(`/search/stream?q=${encodeURIComponent(query)}`);
      const buffer: SearchHit[] = [];

      source.addEventListener("hit", (event) => {
        try {
          buffer.push(JSON.parse((event as MessageEvent).data));
          setHits([...buffer]);
        } catch {
          // ignore a malformed frame; the stream keeps going
        }
      });
      const finish = () => {
        setLoading(false);
        source?.close();
      };
      source.addEventListener("done", finish);
      // EventSource auto-reconnects after a drop; we don't want it re-firing a
      // stale query, so close on the first error and just drop the spinner.
      source.onerror = finish;
    }, DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
      source?.close();
    };
  }, [query]);

  return { hits, loading };
}
