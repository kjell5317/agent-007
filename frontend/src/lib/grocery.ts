import type { GroceryCategory, GroceryState, GroceryUnit } from "./types";

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

// The unit an amount is typed in, next to the edit field.
export function unitLabel(unit: GroceryUnit): string {
  return unit === "g" || unit === "ml" ? unit : COUNT_UNIT[unit][1];
}

export function parseAmount(text: string): number | null {
  const value = Number(text.trim().replace(",", "."));
  return Number.isFinite(value) && value > 0 ? value : null;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Both dates are YYYY-MM-DD; `today` is the user's local day.
export function daysLeft(expiresOn: string, today: string): number {
  return Math.round(
    (Date.parse(`${expiresOn}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / 86_400_000,
  );
}

export function fmtExpiry(expiresOn: string, today: string): string {
  const days = daysLeft(expiresOn, today);
  if (days < 0) return "expired";
  if (days === 0) return "use today";
  if (days === 1) return "use by tomorrow";
  if (days < 7) return `${days} days left`;
  const [, month, day] = expiresOn.split("-").map(Number);
  return `good until ${MONTHS[month - 1]} ${day}`;
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
