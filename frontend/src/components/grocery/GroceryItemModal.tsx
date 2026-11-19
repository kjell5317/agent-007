import { useState } from "react";
import { Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ChoiceChips } from "@/components/ui/choice-chips";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { CATEGORY_LABEL, parseAmount, unitLabel } from "@/lib/grocery";
import type { GroceryCategory, GroceryItem } from "@/lib/types";

interface Props {
  item: GroceryItem;
  onClose: () => void;
  onSave: (patch: { quantity?: number; category?: GroceryCategory }) => Promise<boolean>;
  onDelete: () => Promise<boolean>;
}

export function GroceryItemModal({ item, onClose, onSave, onDelete }: Props) {
  const [amount, setAmount] = useState(String(Math.round(item.quantity * 100) / 100));
  const [category, setCategory] = useState(item.category);
  const [busy, setBusy] = useState(false);
  const quantity = parseAmount(amount);
  const patch = {
    ...(quantity !== null && quantity !== item.quantity ? { quantity } : {}),
    ...(category !== item.category ? { category } : {}),
  };

  const run = async (action: () => Promise<boolean>) => {
    setBusy(true);
    const ok = await action();
    setBusy(false);
    if (ok) onClose();
  };

  return (
    <Modal open onClose={onClose} title={item.name}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (Object.keys(patch).length > 0) void run(() => onSave(patch));
        }}
      >
        <div className="space-y-1.5">
          <label htmlFor="grocery-amount" className="text-xs font-medium text-muted-foreground">
            Amount
          </label>
          <div className="flex items-center gap-2">
            <Input
              id="grocery-amount"
              inputMode="decimal"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              className="h-10"
            />
            <span className="shrink-0 text-sm text-muted-foreground">{unitLabel(item.unit)}</span>
          </div>
        </div>
        <div className="space-y-1.5">
          <span className="text-xs font-medium text-muted-foreground">Category</span>
          <ChoiceChips
            value={category}
            labels={CATEGORY_LABEL}
            disabled={busy}
            onChange={(next) => {
              if (next) setCategory(next);
            }}
          />
        </div>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="ghost"
            onClick={() => void run(onDelete)}
            disabled={busy}
            className="text-destructive hover:text-destructive"
          >
            <Trash2 className="h-4 w-4" />
            Remove
          </Button>
          <Button
            type="submit"
            className="flex-1"
            disabled={busy || quantity === null || Object.keys(patch).length === 0}
          >
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}
