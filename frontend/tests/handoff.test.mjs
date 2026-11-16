import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const { outputText } = ts.transpileModule(
  readFileSync(new URL("../src/lib/handoff.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
);
const exports = {};
vm.runInNewContext(outputText, { exports });
const { handoffChip, handoffActive, manualHandoffKinds, resultList } = exports;

const handoff = (kind, state, result = null) => ({ kind, state, brief: "b", url: null, result });

test("chip text follows the hand-off state", () => {
  assert.equal(handoffChip(null), null);
  assert.equal(handoffChip(handoff("code_change", "declined")), null);
  assert.deepEqual({ ...handoffChip(handoff("code_change", "proposed")) }, { text: "Agent can open an issue", tone: "proposed" });
  assert.equal(handoffChip(handoff("web_research", "running")).text, "Researching…");
  assert.equal(handoffChip(handoff("email_reply", "ready")).text, "Draft ready");
  assert.equal(handoffChip(handoff("own_data_lookup", "failed")).tone, "failed");
});

test("only proposed and running hand-offs block a new one", () => {
  assert.equal(handoffActive(handoff("web_research", "running")), true);
  assert.equal(handoffActive(handoff("code_change", "proposed")), true);
  assert.equal(handoffActive(handoff("web_research", "ready")), false);
  assert.equal(handoffActive(null), false);
});

test("menu offers drafts only for email tasks and issues only without a GitHub link", () => {
  assert.deepEqual([...manualHandoffKinds({ raw_inputs: [{ source: "slack" }], link: null })],
    ["web_research", "own_data_lookup", "code_change"]);
  assert.deepEqual([...manualHandoffKinds({ raw_inputs: [{ source: "gmail" }], link: "https://github.com/a/b/issues/1" })],
    ["web_research", "own_data_lookup", "email_reply"]);
  assert.deepEqual([...manualHandoffKinds({ raw_inputs: [{ source: "gmail" }], link: null, parent_task_id: "p" })],
    ["web_research", "own_data_lookup"]);
});

test("result lists ignore non-string entries", () => {
  assert.deepEqual([...resultList({ open_questions: ["Room?", 3] }, "open_questions")], ["Room?"]);
  assert.deepEqual([...resultList(null, "open_questions")], []);
});
