import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import type { PointAction, PointsData } from "@/lib/types";

function formatPoints(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

export function PointsPanel() {
  const [data, setData] = useState<PointsData | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      setData(await api.getPoints());
    } catch (err) {
      toast.error(`Failed to load points: ${(err as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onTotal = useCallback(
    (total: number) => setData((d) => (d ? { ...d, total } : d)),
    [],
  );

  if (loading && !data) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">Loading…</p>
    );
  }
  if (!data) return null;

  const hasAny = data.sections.some((s) => s.actions.length > 0);

  return (
    <div className="space-y-6">
      <div className="flex flex-col items-center py-6">
        <span className="text-6xl font-bold tabular-nums">
          {formatPoints(data.total)}
        </span>
        <span className="mt-1 text-sm text-muted-foreground">points</span>
      </div>

      {!hasAny && (
        <p className="text-center text-sm text-muted-foreground">
          No actions configured yet — add some in{" "}
          <code className="rounded bg-muted px-1 py-0.5 text-xs">
            config/points.yaml
          </code>
          .
        </p>
      )}

      {data.sections.map((section) => (
        <section key={section.key} className="space-y-2">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {section.title}
          </h2>
          {section.actions.length === 0 ? (
            <p className="text-xs text-muted-foreground">No actions.</p>
          ) : (
            <div className="space-y-2">
              {section.actions.map((action) => (
                <ActionRow
                  key={action.name}
                  section={section.key}
                  action={action}
                  onTotal={onTotal}
                />
              ))}
            </div>
          )}
        </section>
      ))}

      {data.task_done_factor > 0 && (
        <p className="pt-2 text-center text-xs text-muted-foreground">
          Completing a task also earns points (
          {formatPoints(data.task_done_factor)} × estimated minutes).
        </p>
      )}
    </div>
  );
}

function ActionRow({
  section,
  action,
  onTotal,
}: {
  section: string;
  action: PointAction;
  onTotal: (total: number) => void;
}) {
  const hasUnit = action.unit != null;
  const [qty, setQty] = useState("1");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    let quantity: number | undefined;
    if (hasUnit) {
      quantity = Number(qty);
      if (!Number.isFinite(quantity) || quantity <= 0) {
        toast.error("Enter a positive number.");
        return;
      }
    }
    setBusy(true);
    try {
      const res = await api.submitPointAction({
        section,
        name: action.name,
        quantity,
      });
      const gained = action.factor * (hasUnit ? (quantity as number) : 1);
      onTotal(res.total);
      toast.success(`+${formatPoints(gained)} · ${action.name}`);
    } catch (err) {
      toast.error(`Failed: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="flex items-center gap-3 p-3">
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{action.name}</div>
        <div className="text-xs text-muted-foreground">
          ×{formatPoints(action.factor)}
          {hasUnit ? ` per ${action.unit}` : ""}
        </div>
      </div>
      {hasUnit && (
        <div className="flex items-center gap-1.5">
          <Input
            type="number"
            inputMode="decimal"
            min="0"
            step="any"
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            className="h-9 w-20"
            aria-label={`${action.name} ${action.unit}`}
          />
          <span className="text-xs text-muted-foreground">{action.unit}</span>
        </div>
      )}
      <Button size="sm" onClick={submit} disabled={busy}>
        Add
      </Button>
    </Card>
  );
}
