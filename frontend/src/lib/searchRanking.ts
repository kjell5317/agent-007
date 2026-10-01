import type { SearchHit, Task } from "@/lib/types";

const DAY = 86_400_000;

function contactDistance(hit: SearchHit, query: string): number {
  const q = query.trim().toLocaleLowerCase();
  if (!q) return Number.MAX_SAFE_INTEGER;
  const name = hit.title.toLocaleLowerCase();
  const emails = (hit.meta?.emails ?? []).map((value) => value.toLocaleLowerCase());
  const phones = (hit.meta?.phones ?? []).map((value) => value.replace(/\D/g, ""));
  const org = (hit.meta?.org ?? "").toLocaleLowerCase();
  const nameWords = name.split(/\s+/);
  const queryWords = q.split(/\s+/);
  const phoneQuery = q.replace(/\D/g, "");
  if (name === q || emails.includes(q)) return 0;
  if (name.startsWith(q) || emails.some((email) => email.startsWith(q))) return DAY;
  if (queryWords.every((word) => nameWords.some((part) => part.startsWith(word)))) return DAY * 2;
  if (name.includes(q) || (phoneQuery.length >= 3 && phones.some((phone) => phone.includes(phoneQuery)))) return DAY * 3;
  if (emails.some((email) => email.includes(q)) || org.includes(q)) return DAY * 5;
  return DAY * 14;
}

export function searchDistance(hit: SearchHit, tasks: ReadonlyMap<string, Task>, now: number, query = ""): number {
  if (hit.type === "contact") return contactDistance(hit, query);
  const isTask = hit.type === "task";
  const isEvent = hit.type === "document" && hit.source === "calendar";
  const date = isTask ? hit.meta?.due_date ?? tasks.get(hit.id)?.due_date : hit.ts;
  const time = date ? Date.parse(date) : NaN;
  if (!Number.isFinite(time)) return Number.MAX_SAFE_INTEGER;
  if (isTask || isEvent) {
    const today = new Date(now);
    const target = new Date(time);
    return Math.abs(
      new Date(target.getFullYear(), target.getMonth(), target.getDate()).getTime() -
      new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime(),
    );
  }
  return Math.abs(now - time);
}
