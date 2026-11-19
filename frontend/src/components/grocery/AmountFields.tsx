import { useState } from "react";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { amountInput, amountPatch, UNIT_LABEL } from "@/lib/grocery";
import type { GroceryUnit } from "@/lib/types";

const UNITS = Object.keys(UNIT_LABEL) as GroceryUnit[];

// No browser autofill or password-manager overlay on the number fields.
const NO_SUGGESTIONS = {
  autoComplete: "off",
  autoCorrect: "off",
  "data-1p-ignore": true,
  "data-lpignore": "true",
  "data-form-type": "other",
} as const;

export interface AmountDraft {
  name: string;
  amount: string;
  unit: GroceryUnit;
  portion: string;
}

export function useAmountDraft(row: {
  name: string;
  quantity: number;
  unit: GroceryUnit;
  portion: number;
}) {
  const [draft, setDraft] = useState<AmountDraft>({
    name: row.name,
    amount: amountInput(row.quantity),
    unit: row.unit,
    portion: amountInput(row.portion),
  });
  const result = amountPatch(row, draft);
  return { draft, setDraft, patch: result ?? {}, valid: result !== null };
}

export function AmountFields({
  draft,
  onChange,
  amountLabel,
}: {
  draft: AmountDraft;
  onChange: (next: AmountDraft) => void;
  amountLabel: string;
}) {
  return (
    <div className="space-y-4">
      <Field label="Name" htmlFor="grocery-name">
        <Input
          id="grocery-name"
          value={draft.name}
          onChange={(e) => onChange({ ...draft, name: e.target.value })}
          className="h-10"
        />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label={amountLabel} htmlFor="grocery-amount">
          <Input
            id="grocery-amount"
            inputMode="decimal"
            value={draft.amount}
            onChange={(e) => onChange({ ...draft, amount: e.target.value })}
            {...NO_SUGGESTIONS}
            className="h-10"
          />
        </Field>
        <Field label="Unit" htmlFor="grocery-unit">
          <Select
            id="grocery-unit"
            value={draft.unit}
            onChange={(e) =>
              onChange({ ...draft, unit: e.target.value as GroceryUnit })
            }
          >
            {UNITS.map((unit) => (
              <option key={unit} value={unit}>
                {UNIT_LABEL[unit]}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      <Field label="Portion" htmlFor="grocery-portion">
        <div className="flex items-center gap-2">
          <Input
            id="grocery-portion"
            inputMode="decimal"
            value={draft.portion}
            onChange={(e) => onChange({ ...draft, portion: e.target.value })}
            {...NO_SUGGESTIONS}
            className="h-10"
          />
          <span className="w-16 shrink-0 text-sm text-muted-foreground">
            {UNIT_LABEL[draft.unit]}
          </span>
        </div>
      </Field>
    </div>
  );
}

export function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label
        htmlFor={htmlFor}
        className="block text-xs font-medium text-muted-foreground"
      >
        {label}
      </label>
      {children}
    </div>
  );
}
