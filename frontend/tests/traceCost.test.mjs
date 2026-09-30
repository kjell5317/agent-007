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

test("keeps original and manual action costs separate", () => {
  const trace = {
    iterations: [{ llm: {
      provider: "anthropic", model: "claude-opus-4-7",
      usage: { input_tokens: 1000, output_tokens: 100 },
    } }],
    web_search: { llm: {
      provider: "google", model: "gemini-3.5-flash",
      usage: { prompt_tokens: 1000, completion_tokens: 100 },
    } },
    manual_override: { llm: {
      provider: "anthropic", model: "claude-opus-4-7",
      usage: { input_tokens: 500, output_tokens: 100 },
    } },
  };
  assert.equal(exports.estimateTraceCost(trace), "€0.0087");
  assert.equal(exports.estimateTraceCost(trace.manual_override), "€0.0044");
});

test("zero-call and unknown-model traces get explicit costs", () => {
  assert.equal(exports.estimateTraceCost({ branch: "auto_duplicate" }), "€0.00");
  assert.equal(exports.estimateTraceCost({ llm: { model: "other", usage: { input_tokens: 5 } } }), "Unavailable");
});

test("includes estimated embedding and grounded search charges", () => {
  const trace = { web_search: { search_queries: 2 } };
  const embedding = { model: "gemini-embedding-001", estimated_input_tokens: 1000 };
  assert.equal(exports.estimateTraceCost(trace, embedding), "€0.0248");
});
