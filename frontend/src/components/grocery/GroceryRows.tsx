import {
  Amphora,
  Circle,
  Cylinder,
  Droplet,
  Leaf,
  Milk,
  Package,
  Shapes,
  Star,
  Weight,
  type LucideIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  MAX_SEGMENTS,
  expiryVariant,
  fmtAmount,
  fmtExpiry,
  fmtPortion,
  portionSegments,
} from "@/lib/grocery";
import type { GroceryItem, GroceryUnit, StorageItem } from "@/lib/types";
import { cn } from "@/lib/utils";

const UNIT_ICON: Record<GroceryUnit, LucideIcon> = {
  g: Weight,
  ml: Droplet,
  piece: Shapes,
  pack: Package,
  can: Cylinder,
  jar: Amphora,
  bottle: Milk,
  bunch: Leaf,
};

export function StarButton({
  staple,
  name,
  onToggle,
}: {
  staple: boolean;
  name: string;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={staple}
      aria-label={`Restock ${name} when it runs out`}
      title={staple ? "Back on the list when it runs out" : "Put back on the list when it runs out"}
      className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:text-amber-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <Star className={cn("h-4 w-4", staple && "fill-amber-400 text-amber-400")} />
    </button>
  );
}

export function ShoppingRow({
  item,
  onCrossOff,
  onEdit,
  onToggleStaple,
}: {
  item: GroceryItem;
  onCrossOff: () => void;
  onEdit: () => void;
  onToggleStaple: () => void;
}) {
  return (
    <li className="flex items-center gap-1 px-1">
      <button
        type="button"
        onClick={onCrossOff}
        aria-label={`Cross off ${item.name}`}
        className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Circle className="h-5 w-5" />
      </button>
      <button
        type="button"
        onClick={onEdit}
        className="flex min-w-0 flex-1 items-baseline gap-2 py-2.5 text-left"
      >
        <span className="truncate text-sm font-medium">{item.name}</span>
        <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
          {fmtAmount(item.quantity, item.unit)}
        </span>
      </button>
      <StarButton staple={item.staple} name={item.name} onToggle={onToggleStaple} />
    </li>
  );
}

export function StorageRow({
  item,
  today,
  onUse,
  onEdit,
  onToggleStaple,
}: {
  item: StorageItem;
  today: string;
  onUse: () => void;
  onEdit: () => void;
  onToggleStaple: () => void;
}) {
  const UnitIcon = UNIT_ICON[item.unit];
  return (
    <li className="flex items-center gap-1 pl-3 pr-2">
      <button type="button" onClick={onEdit} className="min-w-0 flex-1 space-y-1.5 py-2.5 text-left">
        <span className="block truncate text-sm font-medium">{item.name}</span>
        <span className="flex items-center gap-2">
          <PortionBar quantity={item.quantity} start={item.start_quantity} portion={item.portion} />
          <span className="truncate text-xs tabular-nums text-muted-foreground">
            {fmtAmount(item.quantity, item.unit)}
          </span>
          <Badge variant={expiryVariant(item.expires_on, today)} className="shrink-0">
            {fmtExpiry(item.expires_on, today)}
          </Badge>
        </span>
      </button>
      <StarButton staple={item.staple} name={item.name} onToggle={onToggleStaple} />
      <Button
        size="sm"
        onClick={onUse}
        aria-label={`Use ${fmtAmount(item.portion, item.unit)} of ${item.name}`}
        title={`Use ${fmtAmount(item.portion, item.unit)}`}
        className="h-8 gap-1 rounded-full px-2.5 tabular-nums"
      >
        −{fmtPortion(item.portion, item.unit)}
        <UnitIcon className="h-3.5 w-3.5" />
      </Button>
    </li>
  );
}

// Like a parent issue's progress: one segment per portion, filled while it's
// still there. Past MAX_SEGMENTS it becomes one continuous bar.
function PortionBar({ quantity, start, portion }: { quantity: number; start: number; portion: number }) {
  const { filled, total } = portionSegments(quantity, start, portion);
  return (
    <span
      role="img"
      aria-label={`${filled} of ${total} portions left`}
      className="flex h-2 w-16 shrink-0 gap-0.5"
    >
      {total <= MAX_SEGMENTS ? (
        Array.from({ length: total }, (_, index) => (
          <span
            key={index}
            className={cn("h-full flex-1 rounded-sm", index < filled ? "bg-primary" : "bg-primary/20")}
          />
        ))
      ) : (
        <span className="relative h-full flex-1 overflow-hidden rounded-sm bg-primary/20">
          <span
            className="absolute inset-y-0 left-0 bg-primary"
            style={{ width: `${Math.min(100, (quantity / Math.max(start, quantity)) * 100)}%` }}
          />
        </span>
      )}
    </span>
  );
}
