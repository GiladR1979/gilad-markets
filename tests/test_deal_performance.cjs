"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const series = require("../docs/deal-performance.js");

test("a measured crash and the final adjustment count as one deal", () => {
  const loss = { crashAt: 2, crashPnl: -75954, closed: 4, pnl: -76635 };
  const gain = { closed: 3, pnl: 2500 };
  const rows = [loss, gain];
  const all = series(rows, 0, 5);
  assert.deepEqual(all.points, [
    { t: 2, v: -75954 },
    { t: 3, v: -73454 },
    { t: 4, v: -74135 },
  ]);
  assert.equal(all.total, loss.pnl + gain.pnl);
  assert.deepEqual(all.contributing, rows);
  assert.equal(series(rows, 0, 2).total, -75954);
  const afterCrash = series(rows, 4, 5);
  assert.deepEqual(afterCrash.points, [{ t: 4, v: -681 }]);
  assert.deepEqual(afterCrash.contributing, [loss]);
});

test("an unknown crash amount enters the chart at the first valued endpoint", () => {
  for (const end of [{ closed: 3 }, { markedAt: 3 }]) {
    const loss = { crashAt: 1, ...end, pnl: -100 };
    assert.deepEqual(series([loss], 0, 2).contributing, []);
    assert.deepEqual(series([loss], 1, 3).points, [{ t: 3, v: -100 }]);
  }
});

test("a revalued open loss and a simultaneous close combine without duplication", () => {
  const open = { crashAt: 1, crashPnl: -100, markedAt: 3, pnl: -75 };
  const flat = { closed: 3, pnl: 0 };
  const unchanged = { crashAt: 1, crashPnl: -50, closed: 3, pnl: -50 };
  const result = series([open, flat, unchanged], 2, 3);
  assert.deepEqual(result.points, [{ t: 3, v: 25 }]);
  assert.deepEqual(result.contributing, [open, flat]);
  const all = series([open, flat, unchanged], 0, 3);
  assert.equal(all.total, -125);
  assert.equal(all.contributing.length, 3);
});
