import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { SearchHit, SearchHitType } from "@/lib/types";

const LOCAL_TYPES: readonly SearchHitType[] = ["task", "document"];

export function useLiveSuggestions(value: string): SearchHit[] {
  const [local, setLocal] = useState<SearchHit[]>([]);
  const [external, setExternal] = useState<SearchHit[]>([]);

  useEffect(() => {
    const q = value.trim();
    setLocal([]);
    setExternal([]);
    if (!q) return;

    let cancelled = false;
    const localTimer = window.setTimeout(() => {
      void api.suggest(q, 8, LOCAL_TYPES).then(({ hits }) => {
        if (!cancelled) setLocal(hits.filter((hit) => LOCAL_TYPES.includes(hit.type)));
      }).catch(() => {
        if (!cancelled) setLocal([]);
      });
    }, 150);
    const externalTimer = q.length >= 3 ? window.setTimeout(() => {
      void api.suggestExternal(q, 6).then(({ hits }) => {
        if (!cancelled) setExternal(hits);
      }).catch(() => {
        if (!cancelled) setExternal([]);
      });
    }, 300) : null;

    return () => {
      cancelled = true;
      window.clearTimeout(localTimer);
      if (externalTimer !== null) window.clearTimeout(externalTimer);
    };
  }, [value]);

  return useMemo(() => external.length
    ? [...local.slice(0, 3), ...external.slice(0, 3)]
    : local.slice(0, 6), [local, external]);
}
