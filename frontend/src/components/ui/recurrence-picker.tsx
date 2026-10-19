import { useState } from "react";
import { Minus, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { StepButton } from "@/components/ui/estimation-picker";
import { RECURRENCE_MAX } from "@/lib/tasks";
import type { RecurrenceUnit } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Props {
  unit: RecurrenceUnit | null;
  interval: number | null;
  busy?: boolean;
  onSave: (unit: RecurrenceUnit, interval: number) => void;
  onRemove: () => void;
}

const UNITS: RecurrenceUnit[] = ["day", "week", "month"];

// Unlike the estimate, nothing saves until "Repeat" — every saved change
// replans the series' upcoming instances on the calendar.
export function RecurrencePicker({ unit, interval, busy = false, onSave, onRemove }: Props) {
  const [draftUnit, setDraftUnit] = useState<RecurrenceUnit>(unit ?? "week");
  const [draftInterval, setDraftInterval] = useState(interval ?? 1);
  const max = RECURRENCE_MAX[draftUnit];
  const unchanged = draftUnit === unit && draftInterval === interval;

  const pickUnit = (next: RecurrenceUnit) => {
    setDraftUnit(next);
    setDraftInterval((value) => Math.min(value, RECURRENCE_MAX[next]));
  };

  return (
    <div className="space-y-4">
      <div className="flex justify-center gap-1.5">
        {UNITS.map((option) => (
          <button
            type="button"
            key={option}
            onClick={() => pickUnit(option)}
            className={cn(
              "rounded-full border px-3 py-1 text-xs font-medium capitalize transition-colors",
              draftUnit === option
                ? "border-primary bg-primary text-primary-foreground"
                : "border-input text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            {option}
          </button>
        ))}
      </div>

      <div className="flex flex-col items-center gap-1">
        <span className="text-xs text-muted-foreground">Repeats every</span>
        <div className="flex items-center gap-3">
          <StepButton
            label="Decrease"
            onClick={() => setDraftInterval((value) => value - 1)}
            disabled={draftInterval <= 1}
          >
            <Minus className="h-4 w-4" />
          </StepButton>
          <div
            className="min-w-[8rem] text-center text-3xl font-medium tabular-nums"
            aria-live="polite"
          >
            {draftInterval} {draftInterval === 1 ? draftUnit : `${draftUnit}s`}
          </div>
          <StepButton
            label="Increase"
            onClick={() => setDraftInterval((value) => value + 1)}
            disabled={draftInterval >= max}
          >
            <Plus className="h-4 w-4" />
          </StepButton>
        </div>
      </div>

      <div className="flex justify-between gap-2">
        {unit ? (
          <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={onRemove}
            className="text-muted-foreground hover:text-destructive">
            Don't repeat
          </Button>
        ) : <span />}
        <Button
          type="button"
          size="sm"
          disabled={busy || unchanged}
          onClick={() => onSave(draftUnit, draftInterval)}
        >
          {unit ? "Save" : "Repeat"}
        </Button>
      </div>
    </div>
  );
}
