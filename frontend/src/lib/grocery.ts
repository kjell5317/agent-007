import type {
  GroceryCategory,
  GroceryDiet,
  GroceryState,
  GroceryUnit,
  Recipe,
} from "./types";

// Key order is the walk through a typical store; the list groups in it.
export const CATEGORY_LABEL: Record<GroceryCategory, string> = {
  produce: "Fruit & vegetables",
  bakery: "Bakery",
  dairy: "Dairy & eggs",
  meat_fish: "Meat & fish",
  dry_goods: "Dry goods",
  canned: "Cans & jars",
  spices_baking: "Spices & baking",
  frozen: "Frozen",
  drinks: "Drinks",
  snacks: "Snacks",
  household: "Household",
  other: "Other",
};

export const UNIT_LABEL: Record<GroceryUnit, string> = {
  g: "g",
  ml: "ml",
  piece: "pieces",
  pack: "packs",
  can: "cans",
  jar: "jars",
  bottle: "bottles",
  bunch: "bunches",
};

export const DIET_LABEL: Record<GroceryDiet, string> = {
  vegan: "Vegan",
  vegetarian: "Vegetarian",
  omnivore: "Omnivore",
};

const CATEGORY_ORDER = Object.keys(CATEGORY_LABEL) as GroceryCategory[];

export function groupByCategory<T extends { category: GroceryCategory; name: string }>(
  items: T[],
): Array<{ category: GroceryCategory; items: T[] }> {
  return CATEGORY_ORDER
    .map((category) => ({
      category,
      items: items
        .filter((item) => item.category === category)
        .sort((a, b) => a.name.localeCompare(b.name)),
    }))
    .filter((group) => group.items.length > 0);
}

const COUNT_UNIT: Record<Exclude<GroceryUnit, "g" | "ml">, [string, string]> = {
  piece: ["pc", "pcs"],
  pack: ["pack", "packs"],
  can: ["can", "cans"],
  jar: ["jar", "jars"],
  bottle: ["bottle", "bottles"],
  bunch: ["bunch", "bunches"],
};

const FRACTIONS: Array<[number, string]> = [
  [1 / 4, "¼"], [1 / 3, "⅓"], [1 / 2, "½"], [2 / 3, "⅔"], [3 / 4, "¾"],
];

export function fmtCount(n: number): string {
  const whole = Math.floor(n + 0.005);
  const rest = n - whole;
  if (rest < 0.005) return String(whole);
  const fraction = FRACTIONS.find(([value]) => Math.abs(rest - value) < 0.02)?.[1];
  if (fraction) return whole === 0 ? fraction : `${whole}${fraction}`;
  return String(Math.round(n * 10) / 10);
}

export function fmtAmount(quantity: number, unit: GroceryUnit): string {
  if (unit === "g" || unit === "ml") {
    if (quantity >= 1000) {
      return `${Math.round(quantity / 10) / 100} ${unit === "g" ? "kg" : "l"}`;
    }
    return `${Math.round(quantity)} ${unit}`;
  }
  const [one, many] = COUNT_UNIT[unit];
  return `${fmtCount(quantity)} ${quantity > 1.005 ? many : one}`;
}

// What an edit sheet sends: only the fields that changed, so a new portion
// alone doesn't count as a new amount (which restarts the storage bar). A new
// unit sends both amounts, which are only meaningful in it. Null while a field
// is invalid.
export function amountPatch(
  row: { name: string; quantity: number; unit: GroceryUnit; portion: number },
  draft: { name: string; amount: string; unit: GroceryUnit; portion: string },
): { name?: string; quantity?: number; unit?: GroceryUnit; portion?: number } | null {
  const name = draft.name.trim();
  const quantity = parseAmount(draft.amount);
  const portion = parseAmount(draft.portion);
  if (!name || quantity === null || portion === null) return null;
  const unitChanged = draft.unit !== row.unit;
  // Compared as shown in the field, so rounding alone isn't an edit.
  const shown = (value: number) => parseAmount(amountInput(value));
  return {
    ...(name !== row.name ? { name } : {}),
    ...(unitChanged || quantity !== shown(row.quantity) ? { quantity } : {}),
    ...(unitChanged ? { unit: draft.unit } : {}),
    ...(unitChanged || portion !== shown(row.portion) ? { portion } : {}),
  };
}

// Units written as their standard abbreviation; the others get an icon.
export function isMetric(unit: GroceryUnit): unit is "g" | "ml" {
  return unit === "g" || unit === "ml";
}

// The label on the "use one portion" button: "150 g", "1 l", or the bare
// count next to the unit's icon.
export function fmtPortion(portion: number, unit: GroceryUnit): string {
  return isMetric(unit) ? fmtAmount(portion, unit) : fmtCount(portion);
}

export function parseAmount(text: string): number | null {
  const value = Number(text.trim().replace(",", "."));
  return Number.isFinite(value) && value > 0 ? value : null;
}

// Shown for an amount in an edit field: no float noise like 0.30000000000000004.
export function amountInput(value: number): string {
  return String(Math.round(value * 100) / 100);
}

export const MAX_SEGMENTS = 12;

// Portions left of the amount the bar started from; each used portion empties
// one segment, a part portion counts as one.
export function portionSegments(
  quantity: number,
  start: number,
  portion: number,
): { filled: number; total: number } {
  const total = Math.max(1, Math.ceil(Math.max(start, quantity) / portion - 1e-6));
  const filled = Math.min(total, Math.max(0, Math.ceil(quantity / portion - 1e-6)));
  return { filled, total };
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function fmtDay(iso: string): string {
  const [, month, day] = iso.split("-").map(Number);
  return `${MONTHS[month - 1]} ${day}`;
}

// Both dates are YYYY-MM-DD; `today` is the user's local day.
export function daysLeft(expiresOn: string, today: string): number {
  return Math.round(
    (Date.parse(`${expiresOn}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / 86_400_000,
  );
}

export function fmtExpiry(expiresOn: string, today: string): string {
  const days = daysLeft(expiresOn, today);
  if (days < 0) return "Expired";
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days < 7) return `In ${days} days`;
  return fmtDay(expiresOn);
}

// The due-date pill's colors: red once gone, orange for its last day or two.
export function expiryVariant(expiresOn: string, today: string): "overdue" | "urgent" | "closed" {
  const days = daysLeft(expiresOn, today);
  if (days < 0) return "overdue";
  if (days <= 1) return "urgent";
  return "closed";
}

export function fmtWeek(weekStart: string): string {
  return `Week of ${fmtDay(weekStart)}`;
}

// Recipes arrive newest week first; the order is kept.
export function groupByWeek(recipes: Recipe[]): Array<{ weekStart: string; recipes: Recipe[] }> {
  const groups: Array<{ weekStart: string; recipes: Recipe[] }> = [];
  for (const recipe of recipes) {
    const last = groups[groups.length - 1];
    if (last?.weekStart === recipe.week_start) last.recipes.push(recipe);
    else groups.push({ weekStart: recipe.week_start, recipes: [recipe] });
  }
  return groups;
}

// Optimistic edits, shown until the server's state arrives.
export function withoutItem(state: GroceryState, id: string): GroceryState {
  return { ...state, items: state.items.filter((item) => item.id !== id) };
}

export function withPortionUsed(state: GroceryState, id: string): GroceryState {
  return {
    ...state,
    storage: state.storage.flatMap((row) => {
      if (row.id !== id) return [row];
      const left = row.quantity - row.portion;
      return left > 1e-9 ? [{ ...row, quantity: left }] : [];
    }),
  };
}

export function withStaple(state: GroceryState, productKey: string, staple: boolean): GroceryState {
  const flip = <T extends { product_key: string; staple: boolean }>(row: T): T =>
    row.product_key === productKey ? { ...row, staple } : row;
  return { items: state.items.map(flip), storage: state.storage.map(flip) };
}
