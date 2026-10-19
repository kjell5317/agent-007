import { Minus, Plus } from "lucide-react";
import { StepButton } from "@/components/ui/estimation-picker";
import { RECURRENCE_MAX } from "@/lib/tasks";
import type { RecurrenceUnit } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Props {
  unit: RecurrenceUnit | null;
  interval: number | null;
  busy?: boolean;
  onChange: (unit: RecurrenceUnit, interval: number) => void;
}

const UNITS: RecurrenceUnit[] = ["day", "week", "month"];

// Every pick saves (and replans the series' upcoming instances); picking a
// unit starts repeating. Stopping lives in the surrounding picker's header.
export function RecurrencePicker({ unit, interval, busy = false, onChange }: Props) {
  const steps = unit && interval ? { unit, interval, max: RECURRENCE_MAX[unit] } : null;

  return (
    <div className="space-y-4">
      <div className="flex justify-center gap-1.5">
        {UNITS.map((option) => (
          <button
            type="button"
            key={option}
            aria-pressed={unit === option}
            disabled={busy}
            onClick={() => {
              if (option !== unit) onChange(option, Math.min(interval ?? 1, RECURRENCE_MAX[option]));
            }}
            className={cn(
              "rounded-full border px-3 py-1 text-xs font-medium capitalize transition-colors disabled:pointer-events-none disabled:opacity-50",
              unit === option
                ? "border-primary bg-primary text-primary-foreground"
                : "border-input text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            {option}
          </button>
        ))}
      </div>

      <div className="flex flex-col items-center gap-1">
        <span className="text-xs text-muted-foreground">
          {steps ? "Repeats every" : "Pick a unit to repeat"}
        </span>
        <div className="flex items-center gap-3">
          <StepButton
            label="Decrease"
            onClick={() => { if (steps) onChange(steps.unit, steps.interval - 1); }}
            disabled={busy || !steps || steps.interval <= 1}
          >
            <Minus className="h-4 w-4" />
          </StepButton>
          <div
            className={cn(
              "min-w-[8rem] text-center text-3xl font-medium tabular-nums",
              !steps && "text-muted-foreground",
            )}
            aria-live="polite"
          >
            {steps
              ? `${steps.interval} ${steps.interval === 1 ? steps.unit : `${steps.unit}s`}`
              : "—"}
          </div>
          <StepButton
            label="Increase"
            onClick={() => { if (steps) onChange(steps.unit, steps.interval + 1); }}
            disabled={busy || !steps || steps.interval >= steps.max}
          >
            <Plus className="h-4 w-4" />
          </StepButton>
        </div>
      </div>
    </div>
  );
}
