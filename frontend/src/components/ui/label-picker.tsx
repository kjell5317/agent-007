import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { labelChipClass } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { Label } from "@/lib/types";

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSave: () => void;
  labels: Label[];
}

export function LabelPicker({ value, onChange, onSave, labels }: Props) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const selected = labels.find((l) => l.name === value);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const pick = (name: string) => {
    onChange(name);
    setOpen(false);
  };

  return (
    <div className="space-y-3">
      <div ref={wrapRef} className="relative w-full">
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-haspopup="listbox"
            aria-expanded={open}
            className="flex h-10 w-full items-center justify-between rounded-md border border-input bg-transparent px-3 text-sm shadow-sm transition-colors hover:bg-accent"
          >
            <span className="flex items-center gap-2">
              <span
                aria-hidden
                className={cn(
                  "h-3 w-3 shrink-0 rounded-full",
                  selected
                    ? labelChipClass(selected.color)
                    : "bg-muted-foreground/30",
                )}
              />
              <span className={cn(!selected && "text-muted-foreground")}>
                {selected?.name || "No label"}
              </span>
            </span>
            <ChevronDown
              className={cn(
                "h-4 w-4 shrink-0 text-muted-foreground transition-transform",
                open && "rotate-180",
              )}
            />
          </button>

        {open && (
          <div
            role="listbox"
            className="absolute z-10 mt-1 max-h-60 w-full overflow-y-auto rounded-md border bg-card py-1 shadow-md"
          >
            <Option
              onClick={() => pick("")}
              isSelected={value === ""}
              colorClass="bg-muted-foreground/30"
              name="No label"
              muted
            />
            {labels.map((l) => (
              <Option
                key={l.name}
                onClick={() => pick(l.name)}
                isSelected={value === l.name}
                colorClass={labelChipClass(l.color)}
                name={l.name}
                description={l.description}
              />
            ))}
          </div>
        )}
      </div>

      <Button type="button" onClick={onSave} className="w-full">
        Save
      </Button>
    </div>
  );
}

function Option({
  onClick,
  isSelected,
  colorClass,
  name,
  description,
  muted,
}: {
  onClick: () => void;
  isSelected: boolean;
  colorClass: string;
  name: string;
  description?: string;
  muted?: boolean;
}) {
  return (
    <button
      type="button"
      role="option"
      aria-selected={isSelected}
      onClick={onClick}
      title={description}
      className={cn(
        "flex w-full items-center gap-2 px-3 py-2 text-sm transition-colors hover:bg-accent",
        isSelected && "bg-accent/60",
      )}
    >
      <span
        aria-hidden
        className={cn("h-3 w-3 shrink-0 rounded-full", colorClass)}
      />
      <span className={cn("flex-1 text-left", muted && "text-muted-foreground")}>
        {name}
      </span>
      {isSelected && <Check className="h-3.5 w-3.5 shrink-0 text-primary" />}
    </button>
  );
}
