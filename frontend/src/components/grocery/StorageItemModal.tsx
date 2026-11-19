import { useState } from "react";
import { AmountFields, useAmountDraft } from "@/components/grocery/AmountFields";
import { ModalActions } from "@/components/grocery/ModalActions";
import { Badge } from "@/components/ui/badge";
import { DayPicker } from "@/components/ui/date-picker";
import { Modal } from "@/components/ui/modal";
import type { GroceryItemPatch } from "@/lib/api";
import { expiryVariant, fmtExpiry } from "@/lib/grocery";
import type { StorageItem } from "@/lib/types";

interface Props {
  item: StorageItem;
  today: string;
  onClose: () => void;
  onSave: (patch: GroceryItemPatch & { expires_on?: string }) => Promise<boolean>;
  onRemove: () => Promise<boolean>;
}

export function StorageItemModal({ item, today, onClose, onSave, onRemove }: Props) {
  const { draft, setDraft, patch, valid } = useAmountDraft(item);
  const [expiresOn, setExpiresOn] = useState(item.expires_on);
  const [picking, setPicking] = useState(false);
  const full = { ...patch, ...(expiresOn !== item.expires_on ? { expires_on: expiresOn } : {}) };

  return (
    <Modal open onClose={onClose} title={item.name} className="max-h-[90dvh] overflow-y-auto">
      <form className="space-y-4" onSubmit={(e) => e.preventDefault()}>
        <AmountFields draft={draft} onChange={setDraft} amountLabel="Left" />
        <div className="space-y-2">
          <div className="flex items-center gap-3">
            <span className="text-xs font-medium text-muted-foreground">Good until</span>
            <button
              type="button"
              aria-expanded={picking}
              aria-label={`Good until: ${fmtExpiry(expiresOn, today)}`}
              onClick={() => setPicking((open) => !open)}
              className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Badge variant={expiryVariant(expiresOn, today)} className="px-2.5 py-1 text-sm">
                {fmtExpiry(expiresOn, today)}
              </Badge>
            </button>
          </div>
          {picking && (
            <DayPicker
              value={expiresOn}
              onChange={(next) => {
                setExpiresOn(next);
                setPicking(false);
              }}
            />
          )}
        </div>
        <ModalActions
          canSave={valid && Object.keys(full).length > 0}
          onSave={() => onSave(full)}
          onDelete={onRemove}
          onDone={onClose}
        />
      </form>
    </Modal>
  );
}
