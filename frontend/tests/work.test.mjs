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

test("complete asks for a log only when no work was recorded", () => {
  const idle = { status: "open", running_since: null, worked_minutes: 0 };
  assert.equal(work.needsWorkLog(idle), true);
  assert.equal(work.needsWorkLog({ ...idle, worked_minutes: 20 }), false);
  assert.equal(work.needsWorkLog({ ...idle, running_since: "2026-11-17T11:00:00Z" }), false);
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
