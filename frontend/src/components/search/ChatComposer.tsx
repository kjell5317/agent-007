import { useEffect, useRef } from "react";
import { Search } from "lucide-react";
import { Input } from "@/components/ui/input";

export function ChatComposer({
  value,
  onChange,
  submitted,
  streaming,
  onClose,
  onEnter,
}: {
  value: string;
  onChange: (value: string) => void;
  submitted: boolean;
  streaming: boolean;
  onClose: () => void;
  // `pressed` separates a real Enter from the idle auto-submit below.
  onEnter: (pressed: boolean) => void;
}) {
  const onEnterRef = useRef(onEnter);
  onEnterRef.current = onEnter;

  useEffect(() => {
    if (!value.trim() || submitted || streaming) return;
    const timer = window.setTimeout(() => onEnterRef.current(false), 1000);
    return () => window.clearTimeout(timer);
  }, [value, submitted, streaming]);

  return (
    <div className="relative min-w-0 flex-1">
      <div className="flex h-12 items-center gap-3 rounded-full bg-secondary px-4 shadow-sm">
        <Search className="h-5 w-5 shrink-0 text-muted-foreground" aria-hidden="true" />
        <Input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="Search..."
          enterKeyHint="search"
          autoCapitalize="sentences"
          autoCorrect="off"
          autoComplete="off"
          autoFocus
          aria-label="Search"
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              // Not gated on `submitted`: Enter after the idle auto-submit still counts.
              if (value.trim() && !streaming) onEnter(true);
              event.currentTarget.blur();
            } else if (event.key === "Escape") {
              onClose();
            }
          }}
          className="h-10 min-w-0 flex-1 border-0 bg-transparent px-0 text-sm shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
        />
      </div>
    </div>
  );
}
