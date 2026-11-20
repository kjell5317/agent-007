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

export type WorkStep = "start" | "resume" | "complete" | PauseReason;

function stopSession(task: Task, now: number): Task {
  return { ...task, running_since: null, worked_minutes: Math.round(workedMinutes(task, now)) };
}

// What the server will answer to a work tap, so the tap shows before it does.
export function afterWork(task: Task, step: WorkStep, now: number): Task {
  const at = new Date(now).toISOString();
  switch (step) {
    case "start":
      return { ...task, running_since: at, waiting_since: null, schedule_state: "placed", scheduled_date: at };
    case "resume":
      return { ...task, waiting_since: null, schedule_state: "unplaced" };
    case "complete":
      return { ...stopSession(task, now), status: "closed" };
    case "waiting":
      return { ...stopSession(task, now), schedule_state: "waiting", waiting_since: at, scheduled_date: null };
    default:
      return stopSession(task, now);
  }
}

// Starting a task pauses the one that ran before, on the server as well.
export function withOnlyRunning(tasks: Task[], changed: Task, now: number): Task[] {
  if (!runningSince(changed)) return tasks;
  return tasks.map((task) => (task.id !== changed.id && runningSince(task) ? stopSession(task, now) : task));
}

export interface FocusSession {
  taskId: string;
  since: string;
}

// The server's running_since lands a moment after the tap's own, so a close
// made in between still counts for the session it was made on.
const SAME_SESSION_MS = 60_000;

export function focusClosedFor(
  closed: FocusSession | null,
  task: Pick<Task, "id" | "status" | "running_since">,
): boolean {
  const since = runningSince(task);
  if (!closed || !since || closed.taskId !== task.id) return false;
  return Math.abs(Date.parse(closed.since) - Date.parse(since)) < SAME_SESSION_MS;
}

type FocusTask = Pick<Task, "id" | "status" | "running_since" | "schedule_state">;

// The running task, or else the task on a break taken from the overlay.
export function focusTarget<T extends FocusTask>(
  tasks: T[],
  closed: FocusSession | null,
  onBreak: FocusSession | null,
): { task: T; breakSince: string | null } | null {
  const running = tasks.find((task) => runningSince(task));
  if (running) return focusClosedFor(closed, running) ? null : { task: running, breakSince: null };
  if (!onBreak) return null;
  const task = tasks.find((candidate) => candidate.id === onBreak.taskId);
  if (!task || task.status !== "open" || task.schedule_state === "waiting") return null;
  return { task, breakSince: onBreak.since };
}

// A session started after the break began. The break's own session is
// earlier, so a stale copy of the task still running doesn't end it.
export function breakOver(onBreak: FocusSession, tasks: FocusTask[]): boolean {
  const began = Date.parse(onBreak.since);
  return tasks.some((task) => {
    const since = runningSince(task);
    return since !== null && Date.parse(since) > began;
  });
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
