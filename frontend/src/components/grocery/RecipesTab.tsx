import { useState } from "react";
import { Check, ChevronRight, Loader2, RefreshCw, ThumbsDown, ThumbsUp, type LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { useRecipes } from "@/hooks/useGrocery";
import { api } from "@/lib/api";
import { fmtWeek, groupByWeek } from "@/lib/grocery";
import { toast } from "@/lib/toast";
import type { Recipe, RecipesState } from "@/lib/types";
import { cn } from "@/lib/utils";

export function RecipesTab({ onOpenSettings }: { onOpenSettings: () => void }) {
  const { state, send } = useRecipes();

  if (!state) {
    return (
      <div className="flex justify-center py-12" role="status" aria-label="Loading recipes">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const rate = (recipe: Recipe, rating: -1 | 0 | 1) =>
    void send(() => api.rateRecipe(recipe.id, rating), (s) => withRating(s, recipe.id, rating));

  const done = (recipe: Recipe) => {
    void send(() => api.setRecipeDone(recipe.id, true), (s) => withDone(s, recipe));
    toast.success(`${recipe.title} moved to the archive`, {
      action: { label: "Undo", onClick: () => void send(() => api.setRecipeDone(recipe.id, false)) },
    });
  };

  return (
    <div className="space-y-5">
      <section className="space-y-2" aria-label="This week">
        <div className="flex items-center justify-between px-1">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            This week
          </h2>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void send(api.replanGroceries)}
            disabled={state.planning || state.recipes_per_week === 0}
            className="gap-1.5"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", state.planning && "animate-spin")} />
            {state.planning ? "Planning…" : "Replan"}
          </Button>
        </div>
        {state.error && !state.planning && (
          <p className="rounded-xl border border-destructive/40 p-3 text-sm text-destructive">
            {state.error}
          </p>
        )}
        {state.recipes_per_week === 0 ? (
          <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
            No recipes planned.{" "}
            <button type="button" onClick={onOpenSettings} className="font-medium text-primary underline-offset-4 hover:underline">
              Choose how often you cook
            </button>
          </div>
        ) : state.this_week.length === 0 ? (
          state.planning ? (
            <div className="space-y-2" role="status" aria-label="Planning recipes">
              {Array.from({ length: state.recipes_per_week }, (_, i) => (
                <SkeletonBlock key={i} className="h-14 w-full rounded-xl" />
              ))}
            </div>
          ) : (
            <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
              No recipes for this week yet. Tap Replan.
            </div>
          )
        ) : (
          <ul className={cn("space-y-2", state.planning && "opacity-60")}>
            {state.this_week.map((recipe) => (
              <RecipeCard key={recipe.id} recipe={recipe} onDone={() => done(recipe)} />
            ))}
          </ul>
        )}
      </section>

      {state.archive.length > 0 && (
        <section className="space-y-3" aria-label="Archive">
          <h2 className="px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Archive
          </h2>
          {groupByWeek(state.archive).map((week) => (
            <div key={week.weekStart} className="space-y-2">
              <h3 className="px-1 text-xs text-muted-foreground">{fmtWeek(week.weekStart)}</h3>
              <ul className="space-y-2">
                {week.recipes.map((recipe) => (
                  <RecipeCard key={recipe.id} recipe={recipe} onRate={(rating) => rate(recipe, rating)} />
                ))}
              </ul>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}

function withRating(state: RecipesState, id: string, rating: -1 | 0 | 1): RecipesState {
  return {
    ...state,
    archive: state.archive.map((r) => (r.id === id ? { ...r, rating: rating || null } : r)),
  };
}

function withDone(state: RecipesState, recipe: Recipe): RecipesState {
  return {
    ...state,
    this_week: state.this_week.filter((r) => r.id !== recipe.id),
    archive: [{ ...recipe, done_at: new Date().toISOString() }, ...state.archive],
  };
}

function RecipeCard({
  recipe,
  onRate,
  onDone,
}: {
  recipe: Recipe;
  // This week: cooked, into the archive. The archive: like or dislike.
  onRate?: (rating: -1 | 0 | 1) => void;
  onDone?: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded-xl border bg-card">
      <div className="flex items-center pr-1">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2.5 text-left"
        >
          <ChevronRight
            className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")}
          />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium">{recipe.title}</span>
            <span className="block text-xs text-muted-foreground">
              {recipe.servings} {recipe.servings === 1 ? "serving" : "servings"} ·{" "}
              {recipe.ingredients.length} ingredients
            </span>
          </span>
        </button>
        {onDone && (
          <Button
            size="sm"
            variant="outline"
            onClick={onDone}
            aria-label={`${recipe.title} is cooked`}
            title="Cooked — move it to the archive"
            className="mr-1 gap-1"
          >
            <Check className="h-3.5 w-3.5" />
            Done
          </Button>
        )}
        {onRate && (
          <>
            <RateButton
              icon={ThumbsUp}
              label={`Like ${recipe.title}`}
              active={recipe.rating === 1}
              activeClass="text-emerald-500"
              onClick={() => onRate(recipe.rating === 1 ? 0 : 1)}
            />
            <RateButton
              icon={ThumbsDown}
              label={`Dislike ${recipe.title}`}
              active={recipe.rating === -1}
              activeClass="text-destructive"
              onClick={() => onRate(recipe.rating === -1 ? 0 : -1)}
            />
          </>
        )}
      </div>
      {open && (
        <div className="space-y-4 border-t px-4 py-3 text-sm">
          <ul className="space-y-1">
            {recipe.ingredients.map((ingredient, index) => (
              <li key={index} className="flex gap-3">
                <span className="w-20 shrink-0 tabular-nums text-muted-foreground">{ingredient.amount}</span>
                <span>{ingredient.name}</span>
              </li>
            ))}
          </ul>
          <ol className="list-decimal space-y-1.5 pl-5 marker:text-muted-foreground">
            {recipe.steps.map((step, index) => (
              <li key={index}>{step}</li>
            ))}
          </ol>
        </div>
      )}
    </li>
  );
}

function RateButton({
  icon: Icon,
  label,
  active,
  activeClass,
  onClick,
}: {
  icon: LucideIcon;
  label: string;
  active: boolean;
  activeClass: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "flex h-10 w-10 shrink-0 items-center justify-center rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        active ? activeClass : "text-muted-foreground hover:text-foreground",
      )}
    >
      <Icon className={cn("h-4 w-4", active && "fill-current")} />
    </button>
  );
}
