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
const { handoffChip, handoffActive, manualHandoffKinds, resultList, handoffAsInput, latestHandoff } = exports;

const handoff = (kind, state, result = null) => ({
  id: "h1", kind, state, brief: "b", result, trace: null,
  created_at: "2026-11-16T10:00:00Z", updated_at: "2026-11-16T10:01:00Z",
});
const labels = [{ name: "Aflatoun", github_repo: "TUM-Social-AI/aflaconnect" }, { name: "CSEE", github_repo: null }];

test("chip text follows the hand-off state", () => {
  assert.equal(handoffChip(null), null);
  assert.equal(handoffChip(handoff("code_change", "declined")), null);
  assert.deepEqual({ ...handoffChip(handoff("code_change", "proposed")) }, { text: "Agent can open an issue", tone: "proposed" });
  assert.equal(handoffChip(handoff("research", "running")).text, "Researching…");
  assert.equal(handoffChip(handoff("research", "ready", { completed: true })).text, "Answered");
  assert.equal(handoffChip(handoff("research", "ready", { completed: false })).text, "Researched");
  assert.equal(handoffChip(handoff("email_reply", "ready")).text, "Draft ready");
  assert.equal(handoffChip(handoff("research", "failed")).tone, "failed");
  assert.equal(latestHandoff({ handoffs: [handoff("research", "ready"), handoff("code_change", "declined")] }).kind, "research");
});

test("only proposed and running hand-offs block a new one", () => {
  assert.equal(handoffActive(handoff("research", "running")), true);
  assert.equal(handoffActive(handoff("code_change", "proposed")), true);
  assert.equal(handoffActive(handoff("research", "ready")), false);
  assert.equal(handoffActive(null), false);
});

test("menu offers issues only for labels connected to a repo", () => {
  assert.deepEqual([...manualHandoffKinds({ raw_inputs: [{ source: "slack" }], link: null, label: "CSEE" }, labels)],
    ["research"]);
  assert.deepEqual([...manualHandoffKinds({ raw_inputs: [{ source: "gmail" }], link: null, label: "Aflatoun" }, labels)],
    ["research", "email_reply", "code_change"]);
  assert.deepEqual([...manualHandoffKinds({ raw_inputs: [{ source: "gmail" }], link: "https://github.com/a/b/issues/1", label: "Aflatoun" }, labels)],
    ["research", "email_reply"]);
  assert.deepEqual([...manualHandoffKinds({ raw_inputs: [{ source: "gmail" }], link: null, parent_task_id: "p", label: null }, labels)],
    ["research"]);
});

test("a finished run is listed like a linked input with its trace", () => {
  const trace = { branch: "handoff_research", outcome: "closed", iterations: [] };
  const input = handoffAsInput({ ...handoff("research", "ready", { summary: "- 2026-117", completed: true }), trace }, "t1");
  assert.equal(input.source, "agent");
  assert.equal(input.content, "- 2026-117");
  assert.equal(input.source_metadata.subject, "Research: b");
  assert.equal(input.status, "duplicate");
  assert.equal(input.task_id, "t1");
  assert.equal(input.agent_trace, trace);
});

test("result lists ignore non-string entries", () => {
  assert.deepEqual([...resultList({ open_questions: ["Room?", 3] }, "open_questions")], ["Room?"]);
  assert.deepEqual([...resultList(null, "open_questions")], []);
});
