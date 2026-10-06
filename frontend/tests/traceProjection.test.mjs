import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const { outputText } = ts.transpileModule(
  readFileSync(new URL("../src/lib/projections.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
);
const exports = {};
vm.runInNewContext(outputText, { exports, require: () => ({ toYaml: () => "" }) });

test("projects prose in order with tool calls from failed extraction attempts", () => {
  const trace = {
    outcome: "task_creation_failed",
    iterations: [{ blocks: [
      { type: "tool_use", name: "search_notes", input: { query: "domain email" } },
      { type: "text", text: "No address found" },
    ] }],
  };
  const projected = exports.projectAgentTrace(trace);
  assert.equal(projected.summary.find((field) => field.label === "Decision").value, "task_creation_failed");
  assert.deepEqual(Array.from(projected.tools, (row) => row.name), ["search_notes", "model_response"]);
  assert.equal(projected.tools[1].result, "No address found");
});
