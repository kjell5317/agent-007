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

test("browser and modal Back return from a subtask to its parent", () => {
  const entries = [{ url: "/app", state: null }];
  let index = 0;
  const location = { pathname: "/app", search: "", hash: "" };
  const setUrl = (url) => {
    location.hash = url.includes("#") ? url.slice(url.indexOf("#")) : "";
  };
  const history = {
    get state() { return entries[index].state; },
    pushState: (state, _title, url) => {
      entries.splice(++index, Infinity, { url, state });
      setUrl(url);
    },
    replaceState: (state, _title, url) => {
      entries[index] = { url, state };
      setUrl(url);
    },
    back: () => {
      if (index > 0) setUrl(entries[--index].url);
    },
  };
  const exports = {};
  vm.runInNewContext(outputText, { exports, window: { location, history }, URLSearchParams });

  exports.pushDeepLink({ kind: "task", id: "parent" });
  exports.pushDeepLink({ kind: "task", id: "child" });
  assert.equal(location.hash, "#task/child");

  // The modal Back button uses the same history transition as browser Back.
  assert.equal(exports.backFromDeepLink(), true);
  assert.equal(location.hash, "#task/parent");
  history.back();
  assert.equal(location.hash, "");
  assert.equal(exports.backFromDeepLink(), false);
});

test("an uppercase public task ID in the path opens its task", () => {
  const exports = {};
  vm.runInNewContext(outputText, { exports, window: {}, URLSearchParams });
  assert.deepEqual(
    JSON.parse(JSON.stringify(exports.parseDeepLink({ pathname: "/QRT12", search: "", hash: "" }))),
    { kind: "task", id: "QRT12" },
  );
  assert.equal(exports.parseDeepLink({ pathname: "/app", search: "", hash: "" }), null);
});
