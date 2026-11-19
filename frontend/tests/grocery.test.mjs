import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const { outputText } = ts.transpileModule(
  readFileSync(new URL("../src/lib/grocery.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
);
const grocery = {};
vm.runInNewContext(outputText, { exports: grocery });
// Arrays built inside the vm context carry its own prototypes.
const plain = (value) => JSON.parse(JSON.stringify(value));

test("weights and volumes switch to kg and l from 1000", () => {
  assert.equal(grocery.fmtAmount(150, "g"), "150 g");
  assert.equal(grocery.fmtAmount(1000, "g"), "1 kg");
  assert.equal(grocery.fmtAmount(1250, "g"), "1.25 kg");
  assert.equal(grocery.fmtAmount(200, "ml"), "200 ml");
  assert.equal(grocery.fmtAmount(1500, "ml"), "1.5 l");
});

test("counted units read as fractions with singular and plural", () => {
  assert.equal(grocery.fmtAmount(1, "piece"), "1 pc");
  assert.equal(grocery.fmtAmount(6, "piece"), "6 pcs");
  assert.equal(grocery.fmtAmount(0.5, "pack"), "½ pack");
  assert.equal(grocery.fmtAmount(1.5, "pack"), "1½ packs");
  assert.equal(grocery.fmtAmount(1 / 3, "jar"), "⅓ jar");
  assert.equal(grocery.fmtAmount(2, "bunch"), "2 bunches");
  assert.equal(grocery.fmtAmount(1.2, "bottle"), "1.2 bottles");
});

test("the list groups in store order, names sorted inside a group", () => {
  const groups = grocery.groupByCategory([
    { name: "Rice", category: "dry_goods" },
    { name: "Tomatoes", category: "produce" },
    { name: "Apples", category: "produce" },
    { name: "Soap", category: "household" },
  ]);
  assert.deepEqual(
    plain(groups.map((g) => [g.category, g.items.map((i) => i.name)])),
    [["produce", ["Apples", "Tomatoes"]], ["dry_goods", ["Rice"]], ["household", ["Soap"]]],
  );
});

test("expiry reads like a due date pill", () => {
  const today = "2026-11-18";
  assert.equal(grocery.fmtExpiry("2026-11-17", today), "Expired");
  assert.equal(grocery.fmtExpiry("2026-11-18", today), "Today");
  assert.equal(grocery.fmtExpiry("2026-11-19", today), "Tomorrow");
  assert.equal(grocery.fmtExpiry("2026-11-22", today), "In 4 days");
  assert.equal(grocery.fmtExpiry("2026-11-25", today), "Nov 25");
  assert.equal(grocery.expiryVariant("2026-11-17", today), "overdue");
  assert.equal(grocery.expiryVariant("2026-11-19", today), "urgent");
  assert.equal(grocery.expiryVariant("2026-11-25", today), "closed");
  assert.equal(grocery.daysLeft("2027-03-30", "2027-03-27"), 3); // across DST
});

test("the portion bar counts portions left of what it started with", () => {
  // 1 bottle of juice, ¼ bottle a glass: four full segments.
  assert.deepEqual(plain(grocery.portionSegments(1, 1, 0.25)), { filled: 4, total: 4 });
  assert.deepEqual(plain(grocery.portionSegments(0.75, 1, 0.25)), { filled: 3, total: 4 });
  // 1 kg rice in 150 g portions: the part portion at the end counts as one.
  assert.deepEqual(plain(grocery.portionSegments(1000, 1000, 150)), { filled: 7, total: 7 });
  assert.deepEqual(plain(grocery.portionSegments(100, 1000, 150)), { filled: 1, total: 7 });
  // Raised above where it started: the bar grows with it.
  assert.deepEqual(plain(grocery.portionSegments(3, 2, 1)), { filled: 3, total: 3 });
});

test("the use button abbreviates metric units; an icon carries the others", () => {
  assert.equal(grocery.fmtPortion(150, "g"), "150 g");
  assert.equal(grocery.fmtPortion(1500, "ml"), "1.5 l");
  assert.equal(grocery.isMetric("ml"), true);
  assert.equal(grocery.isMetric("bottle"), false);
  assert.equal(grocery.fmtPortion(0.25, "bottle"), "¼");
  assert.equal(grocery.fmtPortion(1, "piece"), "1");
  assert.equal(grocery.amountInput(0.1 + 0.2), "0.3");
});

test("archived recipes group by week, newest first as they come", () => {
  const groups = grocery.groupByWeek([
    { id: "a", week_start: "2026-11-09" },
    { id: "b", week_start: "2026-11-09" },
    { id: "c", week_start: "2026-11-02" },
  ]);
  assert.deepEqual(
    plain(groups.map((g) => [g.weekStart, g.recipes.map((r) => r.id)])),
    [["2026-11-09", ["a", "b"]], ["2026-11-02", ["c"]]],
  );
  assert.equal(grocery.fmtWeek("2026-11-09"), "Week of Nov 9");
});

test("typed amounts accept a decimal comma", () => {
  assert.equal(grocery.parseAmount("1,5"), 1.5);
  assert.equal(grocery.parseAmount(" 250 "), 250);
  assert.equal(grocery.parseAmount("0"), null);
  assert.equal(grocery.parseAmount("abc"), null);
});

test("optimistic edits: a used portion counts down, the last one leaves", () => {
  const state = {
    items: [],
    storage: [
      { id: "a", product_key: "rice", quantity: 300, portion: 150, staple: false },
      { id: "b", product_key: "milk", quantity: 200, portion: 200, staple: false },
    ],
  };
  const once = grocery.withPortionUsed(state, "a");
  assert.equal(once.storage.find((r) => r.id === "a").quantity, 150);
  assert.deepEqual(plain(grocery.withPortionUsed(state, "b").storage.map((r) => r.id)), ["a"]);
});

test("optimistic edits: a star covers every row of the product", () => {
  const state = {
    items: [{ id: "i", product_key: "milk", staple: false }],
    storage: [
      { id: "s", product_key: "milk", staple: false },
      { id: "t", product_key: "rice", staple: false },
    ],
  };
  const next = grocery.withStaple(state, "milk", true);
  assert.deepEqual(
    plain([...next.items, ...next.storage].map((r) => [r.id, r.staple])),
    [["i", true], ["s", true], ["t", false]],
  );
  assert.deepEqual(plain(grocery.withoutItem(state, "i").items), []);
  assert.deepEqual(plain(grocery.withoutStored(state, "s").storage.map((r) => r.id)), ["t"]);
});

test("an edit sends only what changed; a new unit sends both amounts", () => {
  const row = { name: "Rice", quantity: 850, unit: "g", portion: 150 };
  const draft = { name: "Rice", amount: "850", unit: "g", portion: "150" };
  assert.deepEqual(plain(grocery.amountPatch(row, draft)), {});
  assert.deepEqual(plain(grocery.amountPatch(row, { ...draft, amount: "400" })), { quantity: 400 });
  assert.deepEqual(plain(grocery.amountPatch(row, { ...draft, portion: "100" })), { portion: 100 });
  assert.deepEqual(
    plain(grocery.amountPatch(row, { ...draft, unit: "pack", amount: "1", portion: "0,25" })),
    { quantity: 1, unit: "pack", portion: 0.25 },
  );
  assert.deepEqual(plain(grocery.amountPatch(row, { ...draft, name: " Reis " })), { name: "Reis" });
  assert.equal(grocery.amountPatch(row, { ...draft, amount: "" }), null);
  // Rounding in the field isn't an edit.
  const third = { ...row, quantity: 1000 / 3 };
  assert.deepEqual(plain(grocery.amountPatch(third, { ...draft, amount: "333.33" })), {});
});
