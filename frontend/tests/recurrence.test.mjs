import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

function load(path, require) {
  const { outputText } = ts.transpileModule(
    readFileSync(new URL(path, import.meta.url), "utf8"),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
  );
  const exports = {};
  vm.runInNewContext(outputText, { exports, require, Math });
  return exports;
}

const tasks = load("../src/lib/tasks.ts");
const recurrence = load("../src/lib/recurrence.ts", (name) => {
  if (name === "@/lib/tasks") return tasks;
  throw new Error(`Unexpected import: ${name}`);
});

let nextId = 0;
function task(unit, interval, estimation, extra = {}) {
  nextId += 1;
  return {
    id: `t${nextId}`,
    recurrence_unit: unit,
    recurrence_interval: interval,
    recurrence_series_id: `s${nextId}`,
    estimation,
    parent_card: null,
    parent_task_id: null,
    ...extra,
  };
}

test("a rule lands in the worst week as often as its interval fits into seven days", () => {
  const count = recurrence.worstCaseWeeklyCount;
  assert.equal(count("day", 1), 7);
  assert.equal(count("day", 2), 4);
  assert.equal(count("day", 3), 3);
  assert.equal(count("day", 6), 2);
  assert.equal(count("week", 1), 1);
  assert.equal(count("week", 3), 1);
  assert.equal(count("month", 1), 1);
  assert.equal(count("month", 12), 1);
});

test("the worst week sums every rule at its weekly count", () => {
  const week = recurrence.worstCaseWeek([
    task("day", 1, 10),
    task("week", 1, 60),
    task("week", 2, 30),
    task("month", 1, 45),
    task("month", 12, 120),
  ]);
  assert.deepEqual({ ...week }, { minutes: 70 + 60 + 30 + 45 + 120, series: 5, unestimated: 0 });
});

test("the open instances of one series count once, at the costliest", () => {
  const first = task("day", 1, 15);
  const later = { ...task("day", 1, 25), recurrence_series_id: first.recurrence_series_id };
  assert.deepEqual({ ...recurrence.worstCaseWeek([first, later]) }, { minutes: 7 * 25, series: 1, unestimated: 0 });
});

test("a split occurrence costs its subtasks, which inherit its rule", () => {
  const occurrence = task("week", 1, 90, { is_container: true });
  const subtask = (estimation) => task(null, null, estimation, {
    recurrence_series_id: null, parent_card: occurrence, parent_task_id: occurrence.id,
  });
  const next = { ...task("week", 1, 90), recurrence_series_id: occurrence.recurrence_series_id };
  const week = recurrence.worstCaseWeek([subtask(40), subtask(70), next]);
  assert.deepEqual({ ...week }, { minutes: 110, series: 1, unestimated: 0 });
});

test("one-off tasks add nothing and unestimated series are counted apart", () => {
  const week = recurrence.worstCaseWeek([task(null, null, 30), task("day", 2, null), task("week", 1, 20)]);
  assert.deepEqual({ ...week }, { minutes: 20, series: 2, unestimated: 1 });
  assert.deepEqual({ ...recurrence.worstCaseWeek([]) }, { minutes: 0, series: 0, unestimated: 0 });
});
