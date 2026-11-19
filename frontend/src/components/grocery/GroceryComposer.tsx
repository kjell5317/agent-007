import { useState, type FormEvent } from "react";
import { Loader2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

// The shopping list's add bar, pinned to the bottom like the task Composer.
export function GroceryComposer({ onAdd }: { onAdd: (text: string) => Promise<boolean> }) {
  const [value, setValue] = useState("");
  const [adding, setAdding] = useState(false);
  const hasText = value.trim().length > 0;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!hasText || adding) return;
    setAdding(true);
    if (await onAdd(value.trim())) setValue("");
    setAdding(false);
  };

  return (
    <div className="fixed inset-x-0 bottom-0 z-40 bg-background/90 pb-[env(safe-area-inset-bottom)] backdrop-blur-xl backdrop-saturate-150 supports-[backdrop-filter]:bg-background/75">
      <form onSubmit={submit} autoComplete="off">
        <div className="mx-auto flex max-w-2xl items-center gap-2 px-4 py-3">
          <div className="flex h-12 min-w-0 flex-1 items-center rounded-full bg-secondary px-4 shadow-sm">
            <Input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="Add an item, e.g. 2 kg rice"
              aria-label="Add an item"
              enterKeyHint="send"
              autoCapitalize="sentences"
              autoComplete="off"
              autoCorrect="off"
              // Suppress browser + password-manager autofill overlays on this field.
              name="grocery-item"
              data-1p-ignore
              data-lpignore="true"
              data-form-type="other"
              className="h-10 min-w-0 flex-1 border-0 bg-transparent px-0 text-sm shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
            />
          </div>
          <Button
            type="submit"
            disabled={!hasText || adding}
            aria-label="Add to shopping list"
            title="Add to shopping list"
            className="h-12 w-12 shrink-0 p-0 shadow-sm disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100"
          >
            {adding ? <Loader2 className="h-5 w-5 animate-spin" /> : <Plus className="h-5 w-5" />}
          </Button>
        </div>
      </form>
    </div>
  );
}
