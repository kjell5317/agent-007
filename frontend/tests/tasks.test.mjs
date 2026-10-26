import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const { outputText } = ts.transpileModule(
  readFileSync(new URL("../src/lib/tasks.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
);
const exports = {};
vm.runInNewContext(outputText, { exports });
const now = Date.parse("2026-09-23T12:00:00Z");
const task = {
  status: "open",
  schedule_status: "scheduled",
  scheduled_date: "2026-09-23T10:00:00Z",
  due_date: "2026-09-24T12:00:00Z",
  estimation: 30,
};

test("repeat labels read naturally for one and many units", () => {
  assert.equal(exports.fmtRecurrence("day", 1), "Daily");
  assert.equal(exports.fmtRecurrence("week", 1), "Weekly");
  assert.equal(exports.fmtRecurrence("month", 3), "Every 3 months");
});

test("repeat intervals stop where the next unit takes over", () => {
  assert.deepEqual({ ...exports.RECURRENCE_MAX }, { day: 6, week: 4, month: 12 });
});

test("subtasks of a split occurrence show the occurrence's repeat", () => {
  const occurrence = { recurrence_unit: "week", recurrence_interval: 1, parent_card: null };
  assert.deepEqual({ ...exports.taskRecurrence(occurrence) }, { label: "Weekly", unit: "week", inherited: false });
  assert.deepEqual(
    { ...exports.taskRecurrence({ recurrence_unit: null, recurrence_interval: null, parent_card: occurrence }) },
    { label: "Weekly", unit: "week", inherited: true },
  );
  assert.equal(exports.taskRecurrence({ recurrence_unit: null, recurrence_interval: null, parent_card: null }), null);
});

test("a running task counts the minutes past its booked slot", () => {
  const running = { ...task, scheduled_date: "2026-09-23T11:00:00Z", running_since: "2026-09-23T11:47:00Z" };
  assert.equal(exports.runningOvertime(running, now), 30);
  assert.equal(exports.runningOvertime({ ...running, estimation_corrected: 45 }, now), 15);
  assert.equal(exports.runningOvertime({ ...running, scheduled_date: "2026-09-23T11:45:00Z" }, now), 0);
  assert.equal(exports.runningOvertime({ ...running, running_since: null }, now), null);
  assert.equal(exports.runningOvertime({ ...running, status: "closed" }, now), null);
});

test("only open tasks count as running", () => {
  const since = "2026-09-23T10:30:00Z";
  assert.equal(exports.taskRunningSince({ status: "open", running_since: since }), since);
  assert.equal(exports.taskRunningSince({ status: "closed", running_since: since }), null);
  assert.equal(exports.taskRunningSince({ status: "open", running_since: null }), null);
});
