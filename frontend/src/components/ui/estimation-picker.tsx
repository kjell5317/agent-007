import { Minus, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface Props {
  // Minutes, or null when no estimate is set.
  value: number | null;
  onChange: (next: number | null) => void;
  onSave: () => void;
}

const STEP_MIN = 5;
const PRESETS = [5, 15, 30, 60, 90, 120];

function fmt(value: number | null): string {
  if (value == null) return "—";
  if (value < 60) return `${value} min`;
  const h = Math.floor(value / 60);
  const m = value % 60;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

export function EstimationPicker({ value, onChange, onSave }: Props) {
  const shift = (delta: number) => {
    const base = value ?? 0;
    const next = Math.max(0, base + delta);
    onChange(next === 0 ? null : next);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-col items-center gap-4">
        <div className="flex items-center gap-3">
          <StepButton
            label="Decrease"
            onClick={() => shift(-STEP_MIN)}
            disabled={(value ?? 0) === 0}
          >
            <Minus className="h-4 w-4" />
          </StepButton>
          <div
            className="min-w-[8rem] text-center text-3xl font-medium tabular-nums"
            aria-live="polite"
          >
            {fmt(value)}
          </div>
          <StepButton label="Increase" onClick={() => shift(STEP_MIN)}>
            <Plus className="h-4 w-4" />
          </StepButton>
        </div>

        <div className="flex max-w-xs flex-wrap justify-center gap-1.5">
          {PRESETS.map((m) => {
            const isSelected = value === m;
            return (
              <button
                type="button"
                key={m}
                onClick={() => onChange(m)}
                className={cn(
                  "rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
                  isSelected
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-input text-muted-foreground hover:bg-accent hover:text-foreground",
                )}
              >
                {fmt(m)}
              </button>
            );
          })}
          <button
            type="button"
            onClick={() => onChange(null)}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
              value === null
                ? "border-primary bg-primary text-primary-foreground"
                : "border-input text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            None
          </button>
        </div>
      </div>

      <Button type="button" onClick={onSave} className="w-full">
        Save
      </Button>
    </div>
  );
}

function StepButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className="inline-flex h-9 w-9 items-center justify-center rounded-full border border-input text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}
