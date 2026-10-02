import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const { outputText } = ts.transpileModule(
  readFileSync(new URL("../src/lib/deepLinks.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
);

test("closing and switching task modals do not leave stale task routes in history", () => {
  const entries = ["/app"];
  let index = 0;
  const location = { pathname: "/app", search: "", hash: "" };
  const setUrl = (url) => {
    location.hash = url.includes("#") ? url.slice(url.indexOf("#")) : "";
    location.search = url.includes("?") ? url.slice(url.indexOf("?")) : "";
  };
  const window = {
    location,
    history: {
      pushState: (_state, _title, url) => {
        entries.splice(++index, Infinity, url);
        setUrl(url);
      },
      replaceState: (_state, _title, url) => {
        entries[index] = url;
        setUrl(url);
      },
    },
  };
  const exports = {};
  vm.runInNewContext(outputText, { exports, window, URLSearchParams });

  exports.pushDeepLink({ kind: "task", id: "child" });
  exports.replaceDeepLink({ kind: "task", id: "parent" });
  exports.clearDeepLink();

  assert.deepEqual(entries, ["/app", "/app"]);
  assert.equal(window.location.hash, "");
});
