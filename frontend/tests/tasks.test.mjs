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
