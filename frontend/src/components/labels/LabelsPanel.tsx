import { useCallback, useEffect, useState } from "react";
import { Github, RefreshCw } from "lucide-react";
import { LabelEditModal } from "@/components/labels/LabelEditModal";
import { Button } from "@/components/ui/button";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { primeLabels } from "@/hooks/useLabels";
import { api } from "@/lib/api";
import { labelChipStyle, labelDotStyle } from "@/lib/labels";
import type { Label } from "@/lib/types";

export function LabelsPanel() {
  const [labels, setLabels] = useState<Label[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Label | null>(null);

  // The calendar owns the label set, so pull it on open: a label added or
  // renamed in Calendar shows up here without any extra step. If Google is
  // unreachable, fall back to the local mirror so the descriptions stay
  // editable.
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await api.syncLabels();
      setLabels(rows);
      primeLabels(rows);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      try {
        const rows = await api.listLabels();
        setLabels(rows);
        primeLabels(rows);
      } catch {
        setLabels([]);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const applyEdit = (updated: Label) => {
    const next = labels.map((l) =>
      l.google_id === updated.google_id ? updated : l,
    );
    setLabels(next);
    primeLabels(next);
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          Labels come from your Google Calendar. Describe each one so the agent
          knows when to use it.
        </p>
        <Button
          size="icon"
          variant="ghost"
          onClick={load}
          disabled={loading}
          aria-label="Refresh labels from Google"
          title="Refresh from Google"
        >
          <RefreshCw className={loading ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
        </Button>
      </div>

      {error && (
        <div className="rounded-xl border border-dashed p-3 text-center text-sm text-muted-foreground">
          Couldn't refresh from Google: {error}
        </div>
      )}

      {loading && labels.length === 0 ? (
        <div className="space-y-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <SkeletonBlock key={i} className="h-16 w-full rounded-xl" />
          ))}
        </div>
      ) : labels.length === 0 ? (
        <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
          No labels on this calendar yet. Create them in Google Calendar, then
          refresh.
        </div>
      ) : (
        <ul className="space-y-2">
          {labels.map((label) => (
            <li key={label.google_id}>
              <button
                type="button"
                onClick={() => setEditing(label)}
                className="flex w-full items-start gap-3 rounded-xl border bg-card p-3 text-left transition-colors hover:bg-accent"
              >
                <span
                  aria-hidden
                  className="mt-1 h-3 w-3 shrink-0 rounded-full bg-muted-foreground/30"
                  style={labelDotStyle(label.color)}
                />
                <span className="min-w-0 flex-1 space-y-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <span
                      className="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground"
                      style={labelChipStyle(label.color)}
                    >
                      {label.name}
                    </span>
                    {label.github_repo && (
                      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                        <Github className="h-3 w-3" />
                        {label.github_repo}
                      </span>
                    )}
                  </span>
                  <span className="block text-sm text-muted-foreground">
                    {label.description || (
                      <span className="italic">
                        No description — hidden from the agent
                      </span>
                    )}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {editing && (
        <LabelEditModal
          label={editing}
          onClose={() => setEditing(null)}
          onSaved={applyEdit}
        />
      )}
    </div>
  );
}
