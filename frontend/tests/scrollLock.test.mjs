import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const { outputText } = ts.transpileModule(
  readFileSync(new URL("../src/lib/scrollLock.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
);

function load() {
  const document = { body: { style: { overflow: "" } } };
  const lib = {};
  vm.runInNewContext(outputText, { exports: lib, document });
  return { lib, body: document.body };
}

test("the outer modal closing first doesn't leave the page locked", () => {
  const { lib, body } = load();
  const releaseOuter = lib.lockBodyScroll();
  const releaseInner = lib.lockBodyScroll();
  releaseOuter();
  assert.equal(body.style.overflow, "hidden");
  releaseInner();
  assert.equal(body.style.overflow, "");
});

test("a release called twice counts once", () => {
  const { lib, body } = load();
  const releaseFirst = lib.lockBodyScroll();
  lib.lockBodyScroll();
  releaseFirst();
  releaseFirst();
  assert.equal(body.style.overflow, "hidden");
});
