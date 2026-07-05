// Map Google Calendar event colorIds (1..11) to a Tailwind background + text
// pair so the in-app label chip matches the color the event will get on the
// calendar. Falls back to a neutral chip when the id is unknown or empty.
const COLOR_CLASSES: Record<string, string> = {
  "1": "bg-violet-200 text-violet-900 dark:bg-violet-500/25 dark:text-violet-200",     // Lavender
  "2": "bg-green-200 text-green-900 dark:bg-green-500/25 dark:text-green-200",         // Sage
  "3": "bg-purple-300 text-purple-900 dark:bg-purple-500/25 dark:text-purple-200",     // Grape
  "4": "bg-pink-200 text-pink-900 dark:bg-pink-500/25 dark:text-pink-200",             // Flamingo
  "5": "bg-yellow-200 text-yellow-900 dark:bg-yellow-400/25 dark:text-yellow-100",     // Banana
  "6": "bg-orange-200 text-orange-900 dark:bg-orange-500/25 dark:text-orange-200",     // Tangerine
  "7": "bg-sky-200 text-sky-900 dark:bg-sky-500/25 dark:text-sky-200",                 // Peacock
  "8": "bg-slate-300 text-slate-900 dark:bg-slate-500/35 dark:text-slate-100",         // Graphite
  "9": "bg-blue-300 text-blue-900 dark:bg-blue-500/25 dark:text-blue-200",             // Blueberry
  "10": "bg-emerald-300 text-emerald-900 dark:bg-emerald-500/25 dark:text-emerald-200", // Basil
  "11": "bg-red-200 text-red-900 dark:bg-red-500/25 dark:text-red-200",                // Tomato
};

export function labelChipClass(colorId: string | undefined): string {
  if (!colorId) return "bg-muted text-muted-foreground";
  return COLOR_CLASSES[colorId] ?? "bg-muted text-muted-foreground";
}

// Transparent-background, colored-border+text counterpart used for the
// unselected state of toggle chips (fill = selected, outline = not).
const OUTLINE_CLASSES: Record<string, string> = {
  "1": "border-violet-400 text-violet-700 dark:border-violet-500/50 dark:text-violet-300",   // Lavender
  "2": "border-green-400 text-green-700 dark:border-green-500/50 dark:text-green-300",        // Sage
  "3": "border-purple-400 text-purple-700 dark:border-purple-500/50 dark:text-purple-300",    // Grape
  "4": "border-pink-400 text-pink-700 dark:border-pink-500/50 dark:text-pink-300",            // Flamingo
  "5": "border-yellow-500 text-yellow-700 dark:border-yellow-400/50 dark:text-yellow-200",    // Banana
  "6": "border-orange-400 text-orange-700 dark:border-orange-500/50 dark:text-orange-300",    // Tangerine
  "7": "border-sky-400 text-sky-700 dark:border-sky-500/50 dark:text-sky-300",                // Peacock
  "8": "border-slate-400 text-slate-600 dark:border-slate-500/50 dark:text-slate-200",        // Graphite
  "9": "border-blue-400 text-blue-700 dark:border-blue-500/50 dark:text-blue-300",            // Blueberry
  "10": "border-emerald-400 text-emerald-700 dark:border-emerald-500/50 dark:text-emerald-300", // Basil
  "11": "border-red-400 text-red-700 dark:border-red-500/50 dark:text-red-300",               // Tomato
};

export function labelChipOutlineClass(colorId: string | undefined): string {
  if (!colorId) return "border-input text-muted-foreground";
  return OUTLINE_CLASSES[colorId] ?? "border-input text-muted-foreground";
}
