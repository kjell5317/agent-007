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

test("manual requests differing only by URL scheme share an inbox group", () => {
  const base = {
    source: "manual", source_metadata: {}, task_id: null,
    task_title: null, agent_trace: null, status: "not_task",
  };
  const groups = exports.groupInputs([
    { ...base, id: "first", content: "Research contact address of kjellhanken.de",
      received_at: "2026-09-30T18:23:00Z" },
    { ...base, id: "second", content: "Research contact address of https://kjellhanken.de",
      received_at: "2026-09-30T18:24:00Z" },
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].members.map((row) => row.id).join(","), "second,first");
});

test("subtask inputs group under the parent task", () => {
  const base = {
    content: "Request", agent_trace: null, status: "open",
    received_at: "2026-10-01T10:00:00Z",
  };
  const groups = exports.groupInputs([
    { ...base, id: "parent", source: "gmail", source_metadata: {},
      task_id: "parent-1", task_title: "Prepare proposal" },
    { ...base, id: "child-a", source: "subtask",
      source_metadata: { parent_task_id: "parent-1", parent_title: "Prepare proposal" },
      task_id: "child-1", task_title: "Draft proposal" },
    { ...base, id: "child-b", source: "subtask",
      source_metadata: { parent_task_id: "parent-1", parent_title: "Prepare proposal" },
      task_id: "child-2", task_title: "Send proposal" },
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].key, "task:parent-1");
  assert.equal(groups[0].title, "Prepare proposal");
  assert.equal(groups[0].liveTask.id, "parent");
  assert.equal(groups[0].members.length, 3);
});
