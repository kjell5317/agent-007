// Map Google Calendar event colorIds (1..11) to a Tailwind background + text
// pair so the in-app label chip matches the color the event will get on the
// calendar. Falls back to a neutral chip when the id is unknown or empty.
const COLOR_CLASSES: Record<string, string> = {
  "1": "bg-violet-200 text-violet-900",     // Lavender
  "2": "bg-green-200 text-green-900",       // Sage
  "3": "bg-purple-300 text-purple-900",     // Grape
  "4": "bg-pink-200 text-pink-900",         // Flamingo
  "5": "bg-yellow-200 text-yellow-900",     // Banana
  "6": "bg-orange-200 text-orange-900",     // Tangerine
  "7": "bg-sky-200 text-sky-900",           // Peacock
  "8": "bg-slate-300 text-slate-900",       // Graphite
  "9": "bg-blue-300 text-blue-900",         // Blueberry
  "10": "bg-emerald-300 text-emerald-900",  // Basil
  "11": "bg-red-200 text-red-900",          // Tomato
};

export function labelChipClass(colorId: string | undefined): string {
  if (!colorId) return "bg-muted text-muted-foreground";
  return COLOR_CLASSES[colorId] ?? "bg-muted text-muted-foreground";
}
