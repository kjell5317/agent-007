import { Box, ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { RunCard } from "@/components/runs/RunCard";
import { cn } from "@/lib/utils";
import type { RunsData } from "@/hooks/useRuns";
import type { KotxContainer } from "@/lib/kotx";

export function RunsPanel({ tasks, containers, loading, error, scope, setScope, refresh }: RunsData) {
  const [showContainers, setShowContainers] = useState(false);

  if (error) {
    return (
      <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
        Couldn't reach the runs API.
        <div className="mt-1 text-xs">{error}</div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="inline-flex rounded-lg bg-muted p-0.5 text-sm">
          {(["active", "all"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setScope(s)}
              className={cn(
                "rounded-md px-3 py-1 font-medium capitalize transition-colors",
                scope === s
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {s}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => setShowContainers((v) => !v)}
          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
        >
          {showContainers ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
          <Box className="h-3.5 w-3.5" />
          {containers.length} running
        </button>
      </div>

      <Collapsible open={showContainers}>
        <div className="space-y-2">
          {containers.length === 0 ? (
            <p className="px-1 text-xs text-muted-foreground">No containers running.</p>
          ) : (
            containers.map((c) => <ContainerRow key={c.id} container={c} />)
          )}
        </div>
      </Collapsible>

      {loading && tasks.length === 0 ? (
        <div className="py-8 text-center text-sm text-muted-foreground">Loading…</div>
      ) : tasks.length === 0 ? (
        <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
          No {scope === "active" ? "active " : ""}runs.
        </div>
      ) : (
        <div className="space-y-2">
          {tasks.map((t) => (
            <RunCard key={t.id} task={t} onChanged={refresh} />
          ))}
        </div>
      )}
    </div>
  );
}

function ContainerRow({ container }: { container: KotxContainer }) {
  return (
    <Card>
      <CardContent className="flex items-center gap-2 p-2.5 pl-3">
        <span className="inline-block h-2 w-2 shrink-0 rounded-full bg-emerald-500" />
        <span className="min-w-0 flex-1 truncate font-mono text-xs">{container.name}</span>
        <span className="shrink-0 text-xs text-muted-foreground">{container.status}</span>
      </CardContent>
    </Card>
  );
}
