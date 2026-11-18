import type { PauseReason, Task, WorkSession } from "@/lib/types";

export const PAUSE_REASON_LABEL: Record<PauseReason, string> = {
  break: "Break",
  waiting: "Waiting on someone",
  interrupted: "Interrupted",
};

const END_REASON_LABEL: Record<string, string> = {
  ...PAUSE_REASON_LABEL,
  switched: "Switched task",
  completed: "Completed",
  abandoned: "Forgotten, not counted",
};

export function runningSince(task: Pick<Task, "status" | "running_since">): string | null {
  return task.status === "open" ? task.running_since : null;
}

// Without any recorded work, Complete first asks when the work happened.
export function needsWorkLog(
  task: Pick<Task, "status" | "running_since" | "worked_minutes">,
): boolean {
  return !runningSince(task) && !task.worked_minutes;
}

export function elapsedMs(since: string, now: number): number {
  return Math.max(0, now - Date.parse(since));
}

export function workedMinutes(
  task: Pick<Task, "status" | "running_since" | "worked_minutes">,
  now: number,
): number {
  const since = runningSince(task);
  return task.worked_minutes + (since ? elapsedMs(since, now) / 60_000 : 0);
}

export function sessionMinutes(
  session: Pick<WorkSession, "started_at" | "ended_at">,
  now: number,
): number {
  const end = session.ended_at ? Date.parse(session.ended_at) : now;
  return Math.max(0, (end - Date.parse(session.started_at)) / 60_000);
}

export function sessionLabel(
  session: Pick<WorkSession, "ended_at" | "end_reason" | "source">,
): string {
  if (!session.ended_at) return "Running";
  if (session.source === "retro") return "Logged";
  return END_REASON_LABEL[session.end_reason ?? ""] ?? "Ended";
}

// A stopwatch reading: "4:07", or "1:04:07" from the first hour on.
export function fmtElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  const seconds = String(total % 60).padStart(2, "0");
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${minutes}:${seconds}`;
}

export function fmtMinutes(minutes: number): string {
  const rounded = Math.round(minutes);
  if (rounded < 60) return `${rounded} min`;
  const hours = Math.floor(rounded / 60);
  const rest = rounded % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}
