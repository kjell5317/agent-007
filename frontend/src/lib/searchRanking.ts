import type { SearchHit, Task } from "@/lib/types";

export function searchDistance(hit: SearchHit, tasks: ReadonlyMap<string, Task>, now: number): number {
  if (hit.type === "contact") return Number.POSITIVE_INFINITY;
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
