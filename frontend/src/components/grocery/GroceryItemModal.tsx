import { useState } from "react";
import { AmountFields, Field, useAmountDraft } from "@/components/grocery/AmountFields";
import { ModalActions } from "@/components/grocery/ModalActions";
import { Modal } from "@/components/ui/modal";
import { Select } from "@/components/ui/select";
import type { GroceryItemPatch } from "@/lib/api";
import { CATEGORY_LABEL } from "@/lib/grocery";
import type { GroceryCategory, GroceryItem } from "@/lib/types";

const CATEGORIES = Object.keys(CATEGORY_LABEL) as GroceryCategory[];

interface Props {
  item: GroceryItem;
  onClose: () => void;
  onSave: (patch: GroceryItemPatch & { category?: GroceryCategory }) => Promise<boolean>;
  onDelete: () => Promise<boolean>;
}

export function GroceryItemModal({ item, onClose, onSave, onDelete }: Props) {
  const { draft, setDraft, patch, valid } = useAmountDraft(item);
  const [category, setCategory] = useState(item.category);
  const full = { ...patch, ...(category !== item.category ? { category } : {}) };

  return (
    <Modal open onClose={onClose} title={item.name} className="max-h-[90dvh] overflow-y-auto">
      <form
        className="space-y-4"
        onSubmit={(e) => e.preventDefault()}
      >
        <AmountFields draft={draft} onChange={setDraft} amountLabel="Amount" />
        <Field label="Category" htmlFor="grocery-category">
          <Select
            id="grocery-category"
            value={category}
            onChange={(e) => setCategory(e.target.value as GroceryCategory)}
          >
            {CATEGORIES.map((value) => (
              <option key={value} value={value}>{CATEGORY_LABEL[value]}</option>
            ))}
          </Select>
        </Field>
        <ModalActions
          canSave={valid && Object.keys(full).length > 0}
          onSave={() => onSave(full)}
          onDelete={onDelete}
          onDone={onClose}
        />
      </form>
    </Modal>
  );
}
