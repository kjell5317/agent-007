import { Minus, Plus } from "lucide-react";
import { StepButton } from "@/components/ui/recurrence-picker";
import { cn } from "@/lib/utils";
import { fmtMinutes } from "@/lib/work";

interface Props {
  value: number | null;
  onChange: (next: number | null) => void;
  allowNone?: boolean;
}

// Durations live on the scheduler's 15-minute grid.
const STEP_MIN = 15;
const PRESETS = [15, 30, 45, 60, 90, 120];

export function EstimationPicker({ value, onChange, allowNone = true }: Props) {
  const shift = (delta: number) => {
    const base = value ?? 0;
    // An older off-grid value snaps to the neighbouring quarter hour first.
    const snapped = delta > 0
      ? Math.floor(base / STEP_MIN) * STEP_MIN + STEP_MIN
      : Math.ceil(base / STEP_MIN) * STEP_MIN - STEP_MIN;
    const next = Math.max(allowNone ? 0 : STEP_MIN, snapped);
    onChange(next === 0 ? null : next);
  };

  return (
    <div className="flex flex-col items-center gap-4">
      <div className="flex items-center gap-3">
        <StepButton
          label="Decrease"
          onClick={() => shift(-STEP_MIN)}
          disabled={(value ?? 0) <= (allowNone ? 0 : STEP_MIN)}
        >
          <Minus className="h-4 w-4" />
        </StepButton>
        <div className="min-w-[8rem] text-center text-3xl font-medium tabular-nums" aria-live="polite">
          {value == null ? "—" : fmtMinutes(value)}
        </div>
        <StepButton label="Increase" onClick={() => shift(STEP_MIN)}>
          <Plus className="h-4 w-4" />
        </StepButton>
      </div>

      <div className="flex max-w-xs flex-wrap justify-center gap-1.5">
        {PRESETS.map((minutes) => (
          <PresetChip key={minutes} selected={value === minutes} onClick={() => onChange(minutes)}>
            {fmtMinutes(minutes)}
          </PresetChip>
        ))}
        {allowNone && (
          <PresetChip selected={value === null} onClick={() => onChange(null)}>
            None
          </PresetChip>
        )}
      </div>
    </div>
  );
}

function PresetChip({
  selected,
  onClick,
  children,
}: {
  selected: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
        selected
          ? "border-primary bg-primary text-primary-foreground"
          : "border-input text-muted-foreground hover:bg-accent hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}
