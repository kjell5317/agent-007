import { Circle, Star } from "lucide-react";
import { Button } from "@/components/ui/button";
import { daysLeft, fmtAmount, fmtExpiry } from "@/lib/grocery";
import type { GroceryItem, StorageItem } from "@/lib/types";
import { cn } from "@/lib/utils";

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
  const portion = fmtAmount(item.portion, item.unit);
  return (
    <li className="flex items-center gap-1 pl-3 pr-2">
      <button type="button" onClick={onEdit} className="min-w-0 flex-1 py-2 text-left">
        <span className="block truncate text-sm font-medium">{item.name}</span>
        <span className="block truncate text-xs text-muted-foreground">
          {fmtAmount(item.quantity, item.unit)} left ·{" "}
          <span
            className={cn(
              daysLeft(item.expires_on, today) <= 1 && "font-medium text-amber-600 dark:text-amber-400",
            )}
          >
            {fmtExpiry(item.expires_on, today)}
          </span>
        </span>
      </button>
      <StarButton staple={item.staple} name={item.name} onToggle={onToggleStaple} />
      <Button
        size="sm"
        variant="secondary"
        onClick={onUse}
        aria-label={`Use ${portion} of ${item.name}`}
        className="h-9 min-w-[4.5rem] tabular-nums"
      >
        −{portion}
      </Button>
    </li>
  );
}
