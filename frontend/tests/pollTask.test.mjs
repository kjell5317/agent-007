import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const { outputText } = ts.transpileModule(
  readFileSync(new URL("../src/lib/pollTask.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
);

function setup(getInput) {
  const timers = new Map();
  let nextId = 0;
  let listener;
  const calls = [];
  const exports = {};
  vm.runInNewContext(outputText, {
    exports,
    require(name) {
      if (name === "./api") return { api: { getInput } };
      if (name === "./events") return {
        subscribeEvents(handler) {
          listener = handler;
          return () => { listener = undefined; };
        },
      };
      throw new Error(`Unexpected import: ${name}`);
    },
    window: {
      setTimeout(fn, delay) { timers.set(++nextId, { fn, delay }); return nextId; },
      clearTimeout(id) { timers.delete(id); },
    },
  });
  const handle = exports.pollTaskCreation("raw-1", {
    onSuccess: () => calls.push("success"),
    onFailure: () => calls.push("failure"),
    onTimeout: () => calls.push("timeout"),
  });
  return {
    calls, timers, handle,
    emit(data) { listener?.({ type: "input", data }); },
    async tick(delay) {
      const entry = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, `No timer for ${delay}ms`);
      timers.delete(entry[0]);
      await entry[1].fn();
    },
    get subscribed() { return !!listener; },
  };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const pending = { task_id: null, agent_trace: null };

test("recovers a missed SSE event and cleans up", async () => {
  let input = pending;
  const state = setup(async () => input);
  await flush();
  input = { task_id: "task-1" };
  await state.tick(5000);
  assert.deepEqual(state.calls, ["success"]);
  assert.equal(state.timers.size, 0);
  assert.equal(state.subscribed, false);
});

test("retries a failed status request and detects worker failure", async () => {
  let attempts = 0;
  const state = setup(async () => {
    if (++attempts === 1) throw new Error("offline");
    return { agent_trace: { manual_override: { outcome: "task_creation_failed" } } };
  });
  await flush();
  await state.tick(5000);
  assert.deepEqual(state.calls, ["failure"]);
  assert.equal(state.timers.size, 0);
});

test("SSE resolves immediately and a late fetch cannot restart polling", async () => {
  let resolve;
  const state = setup(() => new Promise((done) => { resolve = done; }));
  state.emit({ id: "other", task_id: "other-task" });
  assert.deepEqual(state.calls, []);
  state.emit({ id: "raw-1", task_id: "task-1" });
  resolve(pending);
  await flush();
  assert.deepEqual(state.calls, ["success"]);
  assert.equal(state.timers.size, 0);
  assert.equal(state.subscribed, false);
});

for (const action of ["cancel", "timeout"]) {
  test(`${action} cleans up and ignores an in-flight response`, async () => {
    let resolve;
    const state = setup(() => new Promise((done) => { resolve = done; }));
    if (action === "cancel") state.handle.cancel();
    else await state.tick(120000);
    resolve({ task_id: "task-1" });
    await flush();
    assert.deepEqual(state.calls, action === "cancel" ? [] : ["timeout"]);
    assert.equal(state.timers.size, 0);
    assert.equal(state.subscribed, false);
  });
}
