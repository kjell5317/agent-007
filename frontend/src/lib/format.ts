import { stringify } from "yaml";

export function toYaml(value: unknown): string {
  return stringify(value, { sortMapEntries: true }).trimEnd();
}

export function formatJsonLikeText(text: string | null): string {
  if (!text) return "";

  const trimmed = text.trim();
  if (!trimmed) return "";

  try {
    return toYaml(JSON.parse(trimmed));
  } catch {
    // Try JSONL below.
  }

  const lines = trimmed.split(/\r?\n/);
  const parsed = [];
  for (const line of lines) {
    const part = line.trim();
    if (!part) continue;
    try {
      parsed.push(JSON.parse(part));
    } catch {
      return text;
    }
  }

  if (parsed.length === 0) return text;
  return parsed.map((entry) => toYaml(entry)).join("\n---\n");
}
