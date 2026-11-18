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
  vm.runInNewContext(outputText, { exports, require, Date });
  return exports;
}

const inbox = load("../src/lib/inbox.ts", () => ({ TERMINAL_STATES: new Set() }));
const { latestHits } = load("../src/lib/latest.ts", (name) => {
  if (name === "@/lib/inbox") return inbox;
  throw new Error(`Unexpected import: ${name}`);
});

const at = (day) => `2026-11-${String(day).padStart(2, "0")}T09:00:00Z`;

function task(id, day, extra = {}) {
  return {
    id, title: id, description: null, link: null, status: "open",
    recurrence_series_id: null, created_at: at(day), ...extra,
  };
}

function input(id, day, extra = {}) {
  return {
    id, source: "gmail", content: id, source_metadata: { subject: id }, received_at: at(day),
    status: "not_task", task_id: null, task_title: null, agent_trace: null, ...extra,
  };
}

function note(id, day, extra = {}) {
  return {
    id, content: id, source: null, source_from: null, needs_review: false,
    created_at: at(day), updated_at: at(20), ...extra,
  };
}

test("tasks, messages and notes mix newest created first", () => {
  const hits = latestHits(
    [task("task-old", 1), task("task-new", 9)],
    [input("mail", 5)],
    [note("note", 7)],
    10,
  );
  assert.deepEqual(Array.from(hits, (hit) => `${hit.type}:${hit.id}`), [
    "task:task-new", "note:note", "input:mail", "task:task-old",
  ]);
  assert.equal(hits[1].ts, at(7), "a note sorts by creation, not its last edit");
});

test("the list stops at the limit", () => {
  const tasks = Array.from({ length: 20 }, (_, n) => task(`t${n}`, n + 1));
  const hits = latestHits(tasks, [], [], 15);
  assert.equal(hits.length, 15);
  assert.equal(hits[0].id, "t19");
});

test("a repeating task shows once, as old as its oldest open instance", () => {
  const hits = latestHits([
    task("daily-1", 3, { recurrence_series_id: "s" }),
    task("daily-2", 10, { recurrence_series_id: "s" }),
    task("one-off", 5),
  ], [], [], 10);
  assert.deepEqual(Array.from(hits, (hit) => hit.id), ["one-off", "daily-1"]);
});

test("messages filed into a task, or awaiting review, and threads collapse", () => {
  const hits = latestHits([], [
    input("filed", 9, { task_id: "t1", status: "open" }),
    input("first", 3, { source_metadata: { subject: "Thread", thread_id: "th" } }),
    input("reply", 4, { source_metadata: { subject: "Thread", thread_id: "th" } }),
  ], [note("draft", 8, { needs_review: true })], 10);
  assert.deepEqual(Array.from(hits, (hit) => hit.id), ["reply"]);
});
