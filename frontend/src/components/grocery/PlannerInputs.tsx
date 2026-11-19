import { useEffect, useState, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { fmtAmount } from "@/lib/grocery";
import type { GroceryPreferences } from "@/lib/types";

// What the weekly plan learns from, exactly as it goes into the prompt.
export function PlannerInputs() {
  const [prefs, setPrefs] = useState<GroceryPreferences | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.groceryPreferences().then(setPrefs, (e) => setError((e as Error).message));
  }, []);

  return (
    <section className="space-y-4 border-t pt-6" aria-label="What the planner learns from">
      <div className="space-y-1">
        <h2 className="text-sm font-semibold">What the planner learns from</h2>
        <p className="text-xs text-muted-foreground">
          Added to every weekly plan as listed here. Storage, your own and starred list
          entries and the recipes you already cooked this week go in completely.
        </p>
      </div>
      {error ? (
        <p className="text-sm text-destructive">{error}</p>
      ) : !prefs ? (
        <div className="flex justify-center py-4" role="status" aria-label="Loading preferences">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      ) : (
        <>
          <Part title="Liked recipes" limit={`the ${prefs.rated_limit} most recent`} empty="Like recipes in the archive.">
            {prefs.liked.length > 0 && <Chips items={prefs.liked} />}
          </Part>
          <Part title="Disliked recipes" limit={`the ${prefs.rated_limit} most recent`} empty="Dislike recipes in the archive.">
            {prefs.disliked.length > 0 && <Chips items={prefs.disliked} />}
          </Part>
          <Part
            title="Usually bought per week"
            limit={`top ${prefs.usual_limit} from the last ${prefs.usual_weeks} weeks`}
            empty="Builds up as you cross things off."
          >
            {prefs.usual.length > 0 && (
              <ul className="divide-y rounded-xl border bg-card text-sm">
                {prefs.usual.map((usual) => (
                  <li key={`${usual.name}-${usual.unit}`} className="flex justify-between gap-3 px-3 py-2">
                    <span className="truncate">{usual.name}</span>
                    <span className="shrink-0 tabular-nums text-muted-foreground">
                      {fmtAmount(usual.quantity, usual.unit)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Part>
          <Part
            title="Often expired before it was used"
            limit={`top ${prefs.expired_limit} from the last ${prefs.expired_weeks} weeks`}
            empty="Nothing went off."
          >
            {prefs.expired.length > 0 && <Chips items={prefs.expired} />}
          </Part>
        </>
      )}
    </section>
  );
}

function Part({
  title,
  limit,
  empty,
  children,
}: {
  title: string;
  limit: string;
  empty: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <h3 className="text-xs font-medium text-muted-foreground">
        {title} <span className="font-normal">· {limit}</span>
      </h3>
      {children || <p className="text-sm text-muted-foreground">{empty}</p>}
    </div>
  );
}

function Chips({ items }: { items: string[] }) {
  return (
    <ul className="flex flex-wrap gap-1.5">
      {items.map((item) => (
        <li key={item} className="rounded-full bg-muted px-2.5 py-1 text-xs">
          {item}
        </li>
      ))}
    </ul>
  );
}
