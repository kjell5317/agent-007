import { useState } from "react";
import { AmountFields, Field, useAmountDraft } from "@/components/grocery/AmountFields";
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
        <Field label="Good until">
          <button
            type="button"
            aria-expanded={picking}
            onClick={() => setPicking((open) => !open)}
            className="block rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Badge variant={expiryVariant(expiresOn, today)} className="px-2.5 py-1 text-sm">
              {fmtExpiry(expiresOn, today)}
            </Badge>
          </button>
          {picking && (
            <DayPicker
              value={expiresOn}
              onChange={(next) => {
                setExpiresOn(next);
                setPicking(false);
              }}
            />
          )}
        </Field>
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
