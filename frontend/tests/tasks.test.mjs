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

test("tasks planned for later sort after every other task", () => {
  const task = (id, fields) => ({
    id,
    schedule_status: "scheduled",
    scheduled_date: null,
    due_date: null,
    created_at: "2026-11-01T00:00:00Z",
    ...fields,
  });
  const tasks = [
    task("deferred-soon", { schedule_status: "deferred", due_date: "2026-11-30T00:00:00Z" }),
    task("planned", { scheduled_date: "2026-11-18T09:00:00Z", due_date: "2026-12-05T00:00:00Z" }),
    task("deferred-late", { schedule_status: "deferred", due_date: "2026-12-10T00:00:00Z" }),
    task("pending", { schedule_status: "pending", due_date: "2026-12-01T00:00:00Z" }),
  ];
  const order = (mode) => [...tasks].sort((a, b) => exports.compareTasks(a, b, mode)).map((t) => t.id);

  assert.deepEqual(order("scheduled"), ["pending", "planned", "deferred-soon", "deferred-late"]);
  assert.deepEqual(order("due"), ["pending", "planned", "deferred-soon", "deferred-late"]);
});

test("only a new task awaiting its first solve shows as planning", () => {
  const task = (fields) => ({ schedule_status: "pending", schedule_state: "placed", is_container: false, ...fields });

  assert.equal(exports.isPlanning(task({})), true);
  assert.equal(exports.isPlanning(task({ schedule_status: "scheduled" })), false);
  assert.equal(exports.isPlanning(task({ schedule_status: "unscheduled" })), false);
  assert.equal(exports.isPlanning(task({ schedule_status: "deferred", schedule_state: "deferred" })), false);
  assert.equal(exports.isPlanning(task({ is_container: true })), false);
  assert.equal(exports.isPlanning(task({ schedule_state: "waiting" })), false);
});

test("only the shopping task reads as the grocery planner's", () => {
  const task = (...sources) => ({ raw_inputs: sources.map((source) => ({ source })) });
  assert.equal(exports.isGroceryTask(task("grocery")), true);
  assert.equal(exports.isGroceryTask(task("gmail", "manual")), false);
  assert.equal(exports.isGroceryTask(task()), false);
});
