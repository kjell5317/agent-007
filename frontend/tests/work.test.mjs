import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const { outputText } = ts.transpileModule(
  readFileSync(new URL("../src/lib/work.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
);
const work = {};
vm.runInNewContext(outputText, { exports: work, Date });
const now = Date.parse("2026-11-17T12:00:00Z");

test("only open tasks count as running", () => {
  const since = "2026-11-17T10:30:00Z";
  assert.equal(work.runningSince({ status: "open", running_since: since }), since);
  assert.equal(work.runningSince({ status: "closed", running_since: since }), null);
  assert.equal(work.runningSince({ status: "open", running_since: null }), null);
});

test("the stopwatch counts up in minutes, then hours", () => {
  assert.equal(work.fmtElapsed(0), "0:00");
  assert.equal(work.fmtElapsed(7_900), "0:07");
  assert.equal(work.fmtElapsed((4 * 60 + 7) * 1000), "4:07");
  assert.equal(work.fmtElapsed((3600 + 4 * 60 + 7) * 1000), "1:04:07");
  assert.equal(work.fmtElapsed(-5_000), "0:00");
});

test("worked time reads in minutes and hours", () => {
  assert.equal(work.fmtMinutes(0), "0 min");
  assert.equal(work.fmtMinutes(44.6), "45 min");
  assert.equal(work.fmtMinutes(60), "1 h");
  assert.equal(work.fmtMinutes(65), "1 h 5 min");
});

test("a running session adds to the finished minutes", () => {
  const task = { status: "open", running_since: "2026-11-17T11:30:00Z", worked_minutes: 20 };
  assert.equal(work.workedMinutes(task, now), 50);
  assert.equal(work.workedMinutes({ ...task, running_since: null }, now), 20);
});

test("sessions are labelled by how they ended", () => {
  const span = { started_at: "2026-11-17T11:00:00Z", ended_at: "2026-11-17T11:25:00Z", source: "live" };
  assert.equal(work.sessionMinutes(span, now), 25);
  assert.equal(work.sessionMinutes({ ...span, ended_at: null }, now), 60);
  assert.equal(work.sessionLabel({ ...span, end_reason: "break" }), "Break");
  assert.equal(work.sessionLabel({ ...span, end_reason: "waiting" }), "Waiting on someone");
  assert.equal(work.sessionLabel({ ...span, end_reason: "switched" }), "Switched task");
  assert.equal(work.sessionLabel({ ...span, end_reason: "completed", source: "retro" }), "Logged");
  assert.equal(work.sessionLabel({ ...span, ended_at: null, end_reason: null }), "Running");
});

const idle = {
  id: "a", status: "open", running_since: null, waiting_since: null, worked_minutes: 20,
  schedule_state: "placed", scheduled_date: "2026-11-17T15:00:00Z",
};
const runningTask = { ...idle, running_since: "2026-11-17T11:30:00Z" };

test("a start shows as running from now, pinned to now", () => {
  const started = work.afterWork({ ...idle, schedule_state: "waiting", waiting_since: "2026-11-17T09:00:00Z" }, "start", now);
  assert.equal(started.running_since, "2026-11-17T12:00:00.000Z");
  assert.equal(started.scheduled_date, "2026-11-17T12:00:00.000Z");
  assert.equal(started.schedule_state, "placed");
  assert.equal(started.waiting_since, null);
});

test("a pause banks the session's minutes", () => {
  const paused = work.afterWork(runningTask, "break", now);
  assert.equal(paused.running_since, null);
  assert.equal(paused.worked_minutes, 50);
  assert.equal(paused.schedule_state, "placed");
  assert.equal(work.afterWork(runningTask, "interrupted", now).worked_minutes, 50);
});

test("waiting takes the task off the calendar until resumed", () => {
  const waiting = work.afterWork(runningTask, "waiting", now);
  assert.equal(waiting.schedule_state, "waiting");
  assert.equal(waiting.waiting_since, "2026-11-17T12:00:00.000Z");
  assert.equal(waiting.scheduled_date, null);
  const resumed = work.afterWork(waiting, "resume", now);
  assert.equal(resumed.schedule_state, "unplaced");
  assert.equal(resumed.waiting_since, null);
});

test("complete closes the task and ends its session", () => {
  const done = work.afterWork(runningTask, "complete", now);
  assert.equal(done.status, "closed");
  assert.equal(done.running_since, null);
  assert.equal(done.worked_minutes, 50);
});

test("starting one task stops the one that ran before", () => {
  const other = { ...runningTask, id: "b" };
  const next = work.afterWork({ ...idle, id: "c" }, "start", now);
  const [a, b] = work.withOnlyRunning([idle, other], next, now);
  assert.equal(a, idle);
  assert.equal(b.running_since, null);
  assert.equal(b.worked_minutes, 50);
  const list = [idle, other];
  assert.equal(work.withOnlyRunning(list, idle, now), list);
});

test("a closed focus overlay stays closed for its session only", () => {
  const closed = { taskId: "a", since: "2026-11-17T11:30:00.000Z" };
  assert.equal(work.focusClosedFor(closed, runningTask), true);
  // The server's start time replacing the tap's own is the same session.
  assert.equal(work.focusClosedFor(closed, { ...runningTask, running_since: "2026-11-17T11:30:02Z" }), true);
  assert.equal(work.focusClosedFor(closed, { ...runningTask, running_since: "2026-11-17T11:45:00Z" }), false);
  assert.equal(work.focusClosedFor(closed, { ...runningTask, id: "b" }), false);
  assert.equal(work.focusClosedFor(null, runningTask), false);
  assert.equal(work.focusClosedFor(closed, idle), false);
});
