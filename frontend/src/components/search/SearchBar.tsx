import { Search, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export function SearchBar({
  value,
  onChange,
  onClose,
}: {
  value: string;
  onChange: (next: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-x-0 bottom-0 z-40 border-t bg-card pb-[env(safe-area-inset-bottom)] shadow-[0_-4px_14px_rgba(15,23,42,0.06)] dark:shadow-[0_-4px_18px_rgba(0,0,0,0.35)]">
      <div className="mx-auto flex max-w-2xl items-center gap-2 px-3 py-2.5">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder="Search everything…"
            enterKeyHint="search"
            autoCapitalize="off"
            autoCorrect="off"
            autoComplete="off"
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Escape") onClose();
            }}
            className={cn(
              "h-10 rounded-full bg-secondary pl-10 text-[15px]",
              value ? "pr-10" : "pr-4",
            )}
          />
          {value && (
            <button
              type="button"
              onClick={() => onChange("")}
              aria-label="Clear search"
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full p-1 text-muted-foreground hover:bg-muted"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
