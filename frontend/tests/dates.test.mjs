import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const { outputText } = ts.transpileModule(
  readFileSync(new URL("../src/lib/dates.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
);
const dates = {};
vm.runInNewContext(outputText, { exports: dates, Date, Intl });
dates.setUserTimezone("Europe/Berlin");

test("configured zone controls displayed wall-clock parts", () => {
  const winter = dates.zonedParts("2026-01-15T08:00:00Z");
  const summer = dates.zonedParts("2026-07-15T07:00:00Z");
  assert.equal(winter.hour, 9);
  assert.equal(summer.hour, 9);
  assert.equal(dates.getUserTimezone(), "Europe/Berlin");
  assert.match(dates.fmtDue("2026-07-15T07:00:00Z"), /09:00/);
});

test("date picker conversion respects standard and daylight saving time", () => {
  assert.equal(
    dates.zonedWallTimeToIso({ year: 2026, month: 1, day: 15, hour: 9, minute: 0 }),
    "2026-01-15T08:00:00.000Z",
  );
  assert.equal(
    dates.zonedWallTimeToIso({ year: 2026, month: 7, day: 15, hour: 9, minute: 0 }),
    "2026-07-15T07:00:00.000Z",
  );
});

test("nonexistent spring clock time advances to the next valid time", () => {
  const iso = dates.zonedWallTimeToIso({ year: 2026, month: 3, day: 29, hour: 2, minute: 30 });
  assert.equal(dates.zonedParts(iso).hour, 3);
  assert.equal(dates.zonedParts(iso).minute, 30);
});

test("a different configured zone controls both display and picker conversion", () => {
  dates.setUserTimezone("America/Los_Angeles");
  const iso = dates.zonedWallTimeToIso({ year: 2026, month: 7, day: 15, hour: 9, minute: 0 });
  assert.equal(iso, "2026-07-15T16:00:00.000Z");
  assert.equal(dates.zonedParts(iso).hour, 9);
  assert.match(dates.fmtDue(iso), /09:00/);
  dates.setUserTimezone("Europe/Berlin");
});
