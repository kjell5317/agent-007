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
  onEnter: (query: string) => void;
}) {
  const onEnterRef = useRef(onEnter);
  onEnterRef.current = onEnter;

  useEffect(() => {
    const query = value.trim();
    if (!query || submitted || streaming) return;
    const timer = window.setTimeout(() => onEnterRef.current(query), 1000);
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
              if (value.trim() && !streaming && !submitted) onEnter(value.trim());
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
