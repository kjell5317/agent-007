import { useEffect, useRef, useState } from "react";
import { Loader2, MapPin, Minus, Plus } from "lucide-react";
import { Field } from "@/components/grocery/AmountFields";
import { Button } from "@/components/ui/button";
import { ChoiceChips } from "@/components/ui/choice-chips";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { DIET_LABEL } from "@/lib/grocery";
import { toast } from "@/lib/toast";
import type { GrocerySettings } from "@/lib/types";

const RECIPE_COUNTS = Object.fromEntries(
  Array.from({ length: 8 }, (_, n) => [String(n), n === 0 ? "None" : String(n)]),
) as Record<string, string>;
const MAX_SERVINGS = 12;

// `replanned` is true when the save changed what gets cooked, which replans
// this week.
export function GrocerySettingsPanel({ onSaved }: { onSaved: (replanned: boolean) => void }) {
  const [saved, setSaved] = useState<GrocerySettings | null>(null);
  const [draft, setDraft] = useState<GrocerySettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [storeFocused, setStoreFocused] = useState(false);
  const suggestionRequest = useRef(0);

  useEffect(() => {
    api.grocerySettings().then(
      (settings) => {
        setSaved(settings);
        setDraft(settings);
      },
      (e) => toast.error((e as Error).message),
    );
  }, []);

  const store = draft?.store_location ?? "";
  useEffect(() => {
    const requestId = ++suggestionRequest.current;
    if (!storeFocused || store.trim().length < 1) {
      setSuggestions([]);
      return;
    }
    const timer = window.setTimeout(() => {
      api.locationSuggestions(store).then(
        ({ suggestions: next }) => {
          if (suggestionRequest.current === requestId) setSuggestions(next.slice(0, 5));
        },
        () => {
          if (suggestionRequest.current === requestId) setSuggestions([]);
        },
      );
    }, 250);
    return () => window.clearTimeout(timer);
  }, [store, storeFocused]);

  if (!draft || !saved) {
    return (
      <div className="flex justify-center py-12" role="status" aria-label="Loading settings">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const cookingChanged =
    draft.diet !== saved.diet ||
    draft.recipes_per_week !== saved.recipes_per_week ||
    draft.servings !== saved.servings;
  const changed = cookingChanged || store.trim() !== (saved.store_location ?? "");

  const save = async () => {
    setBusy(true);
    try {
      const next = await api.updateGrocerySettings({ ...draft, store_location: store.trim() });
      setSaved(next);
      setDraft(next);
      onSaved(cookingChanged);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="space-y-6"
      onSubmit={(e) => {
        e.preventDefault();
        if (changed && !busy) void save();
      }}
    >
      <Field label="Diet">
        <ChoiceChips
          value={draft.diet}
          labels={DIET_LABEL}
          onChange={(diet) => diet && setDraft({ ...draft, diet })}
        />
      </Field>

      <Field label="Recipes per week">
        <ChoiceChips
          value={String(draft.recipes_per_week)}
          labels={RECIPE_COUNTS}
          onChange={(count) => count && setDraft({ ...draft, recipes_per_week: Number(count) })}
        />
        <p className="text-xs text-muted-foreground">How often you cook at home in a week.</p>
      </Field>

      <Field label="Servings">
        <div className="flex items-center gap-3">
          <Button
            type="button"
            size="icon"
            variant="outline"
            aria-label="Fewer servings"
            disabled={draft.servings <= 1}
            onClick={() => setDraft({ ...draft, servings: draft.servings - 1 })}
          >
            <Minus className="h-4 w-4" />
          </Button>
          <span className="w-6 text-center text-sm font-medium tabular-nums" aria-live="polite">
            {draft.servings}
          </span>
          <Button
            type="button"
            size="icon"
            variant="outline"
            aria-label="More servings"
            disabled={draft.servings >= MAX_SERVINGS}
            onClick={() => setDraft({ ...draft, servings: draft.servings + 1 })}
          >
            <Plus className="h-4 w-4" />
          </Button>
        </div>
      </Field>

      <Field label="Store" htmlFor="grocery-store">
        <div className="relative">
          <MapPin className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            id="grocery-store"
            value={store}
            placeholder="Where you usually shop"
            autoComplete="off"
            onChange={(e) => setDraft({ ...draft, store_location: e.target.value })}
            onFocus={() => setStoreFocused(true)}
            onBlur={() => setStoreFocused(false)}
            className="h-10 pl-9"
          />
        </div>
        {suggestions.length > 0 && (
          <ul className="overflow-hidden rounded-md border bg-card text-sm shadow-sm">
            {suggestions.map((suggestion) => (
              <li key={suggestion}>
                <button
                  type="button"
                  // Before the input's blur clears the list.
                  onPointerDown={(e) => e.preventDefault()}
                  onClick={() => {
                    setDraft({ ...draft, store_location: suggestion });
                    setSuggestions([]);
                  }}
                  className="block w-full truncate px-3 py-2 text-left hover:bg-accent"
                >
                  {suggestion}
                </button>
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-muted-foreground">
          A shopping task gets this location, so the trip there is planned too.
        </p>
      </Field>

      <Button type="submit" className="w-full" disabled={!changed || busy}>
        {busy && <Loader2 className="h-4 w-4 animate-spin" />}
        {cookingChanged ? "Save and replan this week" : "Save"}
      </Button>
    </form>
  );
}
