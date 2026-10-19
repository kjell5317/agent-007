import { cn } from "@/lib/utils";

interface Props<T extends string> {
  value: T | null;
  labels: Record<T, string>;
  // Shown under the chips for the selected choice.
  hints?: Record<T, string>;
  // Adds a chip that clears the value.
  noneLabel?: string;
  // Per-choice color for the selected chip, instead of the primary fill.
  selectedClasses?: Partial<Record<T, string>>;
  centered?: boolean;
  disabled?: boolean;
  onChange: (value: T | null) => void;
}

export function ChoiceChips<T extends string>({
  value,
  labels,
  hints,
  noneLabel,
  selectedClasses,
  centered = false,
  disabled = false,
  onChange,
}: Props<T>) {
  const choices: Array<[T | null, string]> = Object.entries(labels) as Array<[T, string]>;
  if (noneLabel) choices.push([null, noneLabel]);

  return (
    <div className="space-y-2">
      <div className={cn("flex flex-wrap gap-1.5", centered && "justify-center")}>
        {choices.map(([choice, text]) => (
          <button
            type="button"
            key={choice ?? ""}
            aria-pressed={value === choice}
            title={choice && hints ? hints[choice] : undefined}
            disabled={disabled}
            onClick={() => { if (choice !== value) onChange(choice); }}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs font-medium transition-colors disabled:pointer-events-none disabled:opacity-50",
              value !== choice
                ? "border-input text-muted-foreground hover:bg-accent hover:text-foreground"
                : (choice && selectedClasses?.[choice]) || "border-primary bg-primary text-primary-foreground",
            )}
          >
            {text}
          </button>
        ))}
      </div>
      {value && hints && (
        <p className={cn("text-xs text-muted-foreground", centered && "text-center")}>
          {hints[value]}
        </p>
      )}
    </div>
  );
}
