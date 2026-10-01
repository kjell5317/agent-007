import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const { outputText } = ts.transpileModule(
  readFileSync(new URL("../src/lib/inbox.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
);
const exports = {};
vm.runInNewContext(outputText, {
  exports,
  require: () => ({ TERMINAL_STATES: new Set() }),
});

test("dismissed task retains its follow-up group and reopen anchor", () => {
  const base = {
    source: "gmail",
    source_metadata: {},
    content: "Request",
    task_id: "task-1",
    task_title: "Original task",
    agent_trace: null,
  };
  const groups = exports.groupInputs([
    { ...base, id: "older", status: "open", received_at: "2026-09-01T10:00:00Z" },
    { ...base, id: "dismissed", status: "not_task", received_at: "2026-09-02T10:00:00Z" },
    { ...base, id: "update", status: "duplicate", received_at: "2026-09-03T10:00:00Z",
      agent_trace: { outcome: "updated" } },
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].key, "task:task-1");
  assert.equal(groups[0].members.length, 3);
  assert.equal(groups[0].dismissedTask.id, "dismissed");
  assert.equal(groups[0].liveTask, null);
  assert.equal(groups[0].title, "Original task");
});
