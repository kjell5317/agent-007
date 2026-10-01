import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const { outputText } = ts.transpileModule(
  readFileSync(new URL("../src/lib/searchRanking.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
);
const ranking = {};
vm.runInNewContext(outputText, { exports: ranking, Date, Math, Number });

const now = new Date(2026, 9, 1, 12).getTime();
const day = 86_400_000;

test("task and event dates use distance from today, not update time", () => {
  const tasks = new Map();
  const task = { type: "task", id: "1", ts: "2026-09-30T11:00:00Z", meta: { due_date: "2026-10-02T22:00:00Z" } };
  const event = { type: "document", source: "calendar", ts: "2026-09-30T08:00:00Z" };
  assert.equal(ranking.searchDistance(task, tasks, now), day);
  assert.equal(ranking.searchDistance(event, tasks, now), day);
});

test("notes, files and GitHub use last modified", () => {
  const tasks = new Map();
  const recent = "2026-10-01T11:00:00Z";
  const old = "2026-09-29T11:00:00Z";
  const note = { type: "note", ts: recent };
  const file = { type: "drive", ts: old };
  const issue = { type: "github", ts: recent };
  assert.equal(ranking.searchDistance(note, tasks, now), ranking.searchDistance(issue, tasks, now));
  assert.ok(ranking.searchDistance(note, tasks, now) < ranking.searchDistance(file, tasks, now));
});

test("contact match quality interleaves contacts with dated results", () => {
  const tasks = new Map();
  const exact = { type: "contact", title: "Alice Smith", meta: { emails: ["alice@example.com"] } };
  const prefix = { type: "contact", title: "Alice Johnson", meta: { emails: [] } };
  const weak = { type: "contact", title: "Bob", meta: { org: "Alice's Office" } };
  const twoDayOldFile = { type: "drive", ts: new Date(now - day * 2).toISOString() };
  assert.equal(ranking.searchDistance(exact, tasks, now, "Alice Smith"), 0);
  assert.ok(ranking.searchDistance(prefix, tasks, now, "Alice") < ranking.searchDistance(twoDayOldFile, tasks, now));
  assert.ok(ranking.searchDistance(weak, tasks, now, "Alice") > ranking.searchDistance(twoDayOldFile, tasks, now));
  assert.equal(ranking.searchDistance(exact, tasks, now, "alice@example.com"), 0);
});
