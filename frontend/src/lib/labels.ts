import type { CSSProperties } from "react";

// Labels carry the hex background color Google gives them, so chips render the
// exact color the event will have on the calendar. Ink is derived instead of
// configured: the palette is user-chosen and spans near-black to pale yellow,
// so no static pair reads on all of it.
//
// Callers keep their neutral Tailwind classes as the base and spread the style
// on top — an unknown or missing color yields `{}`, leaving the neutral look.

type Rgb = [number, number, number];

// Threshold where white and near-black ink have equal WCAG contrast against
// the background: (L + 0.05)² = 1.05 × 0.0793.
const INK_CROSSOVER = 0.24;
const DARK_INK: Rgb = [31, 41, 55];
const WHITE: Rgb = [255, 255, 255];
const BLACK: Rgb = [0, 0, 0];

export function labelChipStyle(color: string | undefined | null): CSSProperties {
  const rgb = parseHex(color);
  if (!rgb) return {};
  return {
    backgroundColor: css(rgb),
    color: css(luminance(rgb) > INK_CROSSOVER ? DARK_INK : WHITE),
  };
}

// Transparent background, colored border + text — the unselected state of a
// toggle chip (fill = selected, outline = not).
export function labelChipOutlineStyle(color: string | undefined | null): CSSProperties {
  const rgb = parseHex(color);
  if (!rgb) return {};
  return { borderColor: css(rgb), color: css(readableInk(rgb)) };
}

// Just the color, for the dots in the label picker.
export function labelDotStyle(color: string | undefined | null): CSSProperties {
  const rgb = parseHex(color);
  return rgb ? { backgroundColor: css(rgb) } : {};
}

function parseHex(color: string | undefined | null): Rgb | null {
  const match = /^#?([0-9a-f]{6})$/i.exec((color ?? "").trim());
  if (!match) return null;
  const n = Number.parseInt(match[1], 16);
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
}

function css([r, g, b]: Rgb): string {
  return `rgb(${r} ${g} ${b})`;
}

// WCAG relative luminance.
function luminance([r, g, b]: Rgb): number {
  const channel = (value: number) => {
    const s = value / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

// Used as text on the page background rather than on its own fill, where an
// extreme label color would vanish in one of the two themes (a black label in
// dark mode, a pale one in light mode). Pull those into a mid band.
function readableInk(rgb: Rgb): Rgb {
  const l = luminance(rgb);
  if (l < 0.08) return mix(rgb, WHITE, 0.45);
  if (l > 0.7) return mix(rgb, BLACK, 0.35);
  return rgb;
}

function mix(from: Rgb, to: Rgb, amount: number): Rgb {
  return from.map((c, i) => Math.round(c + (to[i] - c) * amount)) as Rgb;
}
