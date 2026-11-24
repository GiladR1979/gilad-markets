"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
require("../docs/deal-apy.js");
const { calculate: withFunding, tierFor } = globalThis.dealApy;
const calculate = (rows) => withFunding(rows, []);
const start = 1700000000;
const day = 86400;
const year = 365 * day;
const deal = (equity, pnl, opened = start, closed = start + year) => ({
  history: { opened, openingEquity: { at: opened, value: equity } },
  closed,
  pnl,
});

test("uses one opening bankroll for the cohort and the actual close dates", () => {
  const loss = { ...deal(50000, -2500, start + day), crashAt: start + day * 2 };
  const gain = deal(200000, 22500, start, start + day * 10);
  gain.history.stake = 123;
  const open = {
    history: { opened: start - year, stake: 900000 },
    markedAt: start,
    pnl: -4000,
  };
  const value = calculate([loss, open, gain]);
  assert.equal(value.percent, 10);
  assert.equal(value.equity, 200000);
  assert.equal(value.equityAt, start);
  assert.equal(value.pnl, 20000);
  assert.equal(value.count, 2);
  assert.equal(value.provisionalCount, 1);
  assert.equal(value.start, start);
  assert.equal(value.end, start + year);
  assert.equal(value.days, 365);
  assert.equal(value.text, "10%");
  assert.equal(calculate([gain, loss]).percent, 10);
});

test("same-time openings select one latest agreeing observation", () => {
  const old = deal(100, 1),
    latest = deal(200, 1),
    conflict = deal(300, 1);
  old.history.openingEquity.at -= 10;
  assert.equal(calculate([old, latest]).equity, 200);
  assert.equal(calculate([latest, old]).equity, 200);
  assert.equal(calculate([latest, latest]).equity, 200);
  for (const tied of [
    [old, latest, conflict],
    [conflict, latest, old],
  ]) {
    const value = calculate(tied);
    assert.equal(value.reason, "missing-equity");
    assert.equal(value.percent, null);
  }
});

test("compounds from the observed equity timestamp even when the first fill is later", () => {
  const r = deal(100, 10, start + day, start + year);
  r.history.openingEquity.at = start;
  const result = calculate([r]);
  assert.equal(result.percent, 10);
  assert.equal(result.start, start);
  assert.equal(result.firstOpened, start + day);
  assert.equal(result.days, 365);
});

test("missing opening bankroll never falls back to turnover or later balances", () => {
  const missing = deal(100, 4),
    future = deal(300, -1),
    later = deal(1000, 3, start + day);
  delete missing.history.openingEquity;
  missing.history.stake = 500;
  future.history.openingEquity.at = start + 1;
  const value = calculate([missing, future, later]);
  assert.equal(value.reason, "missing-equity");
  assert.equal(value.pnl, 6);
  assert.equal(value.count, 3);
  assert.equal(value.start, start);
  assert.equal(value.end, start + year);
  assert.equal(value.days, 365);
  assert.equal(value.equity, null);
  assert.equal(value.tier, null);
  assert.equal(calculate([deal(0, 0)]).reason, "invalid-return");
});

test("annualizes fractional periods by compounding the bankroll return", () => {
  const halfYear = calculate([deal(100000, 10000, start, start + year / 2)]);
  assert.ok(Math.abs(halfYear.percent - 21) < 1e-12);
  assert.equal(halfYear.days, 182.5);
  assert.equal(halfYear.text, "21%");
  assert.equal(halfYear.periodText, "+10%");
});

test("shows the signed observed return when APY is unavailable", () => {
  assert.equal(calculate([deal(100, -177)]).periodText, "−177%");
  assert.equal(calculate([deal(100, 0)]).periodText, "0%");
  assert.equal(calculate([deal(100, 4, start, start)]).periodText, "+4%");
  assert.equal(calculate([deal(100000, 1)]).periodText, "+1.00e−3%");
  assert.equal(calculate([deal(0, 1)]).periodText, null);
  assert.equal(
    calculate([{ history: { opened: start }, pnl: 5, closed: start + year }])
      .periodText,
    null,
  );
});

test("zero yield, total loss and unavailable estimates are distinct", () => {
  assert.equal(calculate([deal(100, 0)]).text, "0%");
  assert.equal(calculate([deal(100, 0)]).tier, "unhoused");
  assert.equal(calculate([deal(100, -100)]).text, "−100%");
  assert.equal(calculate([deal(100, -101)]).reason, "invalid-return");
  assert.equal(calculate([deal(100, 1, start, start)]).reason, "no-duration");
  assert.equal(calculate([]).reason, "no-closed-deals");
  const openOnly = calculate([{ markedAt: start, pnl: -100 }]);
  assert.equal(openOnly.provisionalCount, 1);
  assert.equal(openOnly.tier, null);
  const missing = calculate([deal(100, 1), { pnl: -5, closed: start }]);
  assert.equal(missing.reason, "missing-history");
  assert.equal(missing.count, 2);
  assert.equal(missing.missingCount, 1);
  assert.equal(missing.text, "—");
  assert.equal(missing.percent, null);
  const reversed = calculate([deal(100, 1, start, start - 1)]);
  assert.equal(reversed.reason, "missing-history");
});

test("all seven portraits use the unrounded threshold and a strict upper two cutover", () => {
  assert.deepEqual(
    [
      -100, -0.1, 0, 49.999, 50, 99.999, 100, 299.999, 300, 1000, 1000.001,
      10000, 10000.001,
    ].map(tierFor),
    [
      "skeleton",
      "skeleton",
      "unhoused",
      "unhoused",
      "anime",
      "anime",
      "sweatpants",
      "sweatpants",
      "programmer",
      "programmer",
      "tokyo",
      "tokyo",
      "commander",
    ],
  );
  for (const boundary of [50, 100, 300, 1000, 10000]) {
    assert.equal(calculate([deal(100, boundary)]).tier, tierFor(boundary));
  }
});

test("compounding across years preserves the exact portrait cutoff and its neighbors", () => {
  const equity = 10000;
  for (const years of [2, 3]) {
    for (const cutoff of [50, 100, 300, 1000, 10000]) {
      const pnl = equity * ((1 + cutoff / 100) ** years - 1);
      const result = (profit) =>
        calculate([deal(equity, profit, start, start + years * year)]);
      const at = result(pnl);
      const below = result(pnl - 1);
      const above = result(pnl + 1);
      assert.equal(at.percent, cutoff);
      assert.equal(at.tier, tierFor(cutoff));
      assert.ok(below.percent < cutoff && above.percent > cutoff);
      assert.equal(below.tier, tierFor(cutoff - 0.001));
      assert.equal(above.tier, tierFor(cutoff + 0.001));
    }
  }
});

test("short profitable or losing trades have meaningful labels at floating-point limits", () => {
  const doubled = calculate([deal(100, 100, start, start + day)]);
  assert.ok(Math.abs(doubled.percent / (100 * 2 ** 365) - 1) < 1e-12);
  assert.equal(doubled.text, "7.52e+111%");
  const extreme = calculate([deal(100, 100, start, start + 1)]);
  assert.equal(extreme.percent, Infinity);
  assert.equal(extreme.tier, "commander");
  const exp = Math.floor(Math.log10(2) * year + 2);
  assert.match(
    extreme.text,
    new RegExp("^[1-9](?:[.][0-9]{1,2})?e[+]" + exp + "%$"),
  );
  assert.equal(calculate([deal(100, -50, start, start + 1)]).text, "≈−100%");
});

test("requires complete funding evidence and reports the selected window only", () => {
  const rows = [deal(1000, 120)];
  assert.equal(withFunding(rows).reason, "missing-flows");
  assert.equal(withFunding(rows, null).netFlows, null);
  assert.equal(
    withFunding(rows, [{ at: start, amount: null }]).flowCount,
    null,
  );
  const value = withFunding(rows, [
    { at: start - 1, amount: 500 },
    { at: start, amount: 500 },
    { at: start + year, amount: 300 },
    { at: start + year + 1, amount: 700 },
  ]);
  assert.equal(value.percent, 12);
  assert.equal(value.flowCount, 1);
  assert.equal(value.netFlows, 300);
});

test("deposits earn the rate only after they arrive", () => {
  // After year one $100 has grown to $110. Add $50, then grow to $176.
  const rows = [deal(10000, 2600, start, start + 2 * year)];
  const value = withFunding(rows, [{ at: start + year, amount: 5000 }]);
  assert.ok(Math.abs(value.percent - 10) < 1e-10);
  assert.equal(value.periodText, "+21%");
  const split = withFunding(rows, [
    { at: start + year, amount: 7000 },
    { at: start + year, amount: -2000 },
  ]);
  assert.equal(split.percent, value.percent);
  assert.equal(split.flowCount, 2);
  assert.equal(split.netFlows, 5000);
});

test("funded exact annual portrait boundaries and tiny material returns stay stable", () => {
  const boundary = withFunding(
    [deal(100, 400)],
    [{ at: start + year / 2, amount: 100 }],
  );
  assert.equal(boundary.percent, 300);
  assert.equal(boundary.tier, "programmer");
  const tiny = withFunding(
    [deal(8e15, 1)],
    [{ at: start + year / 2, amount: 100 }],
  );
  assert.ok(Math.abs(tiny.percent / (100 / (8e15 + 50)) - 1) < 1e-12);
});

test("supports withdrawals, including alternating deposits, losses and a full payout", () => {
  const mixed = withFunding(
    [deal(100, 690, start, start + 3 * year)],
    [
      { at: start + year, amount: -20 },
      { at: start + 2 * year, amount: 50 },
    ],
  );
  assert.equal(mixed.percent, 100);
  assert.equal(mixed.periodText, "+700%");
  const loss = withFunding(
    [deal(10000, -6500, start, start + 2 * year)],
    [{ at: start + year, amount: -2000 }],
  );
  assert.ok(Math.abs(loss.percent + 50) < 1e-10);
  const payout = withFunding(
    [deal(100, 10, start, start + 2 * year)],
    [{ at: start + year, amount: -110 }],
  );
  assert.ok(Math.abs(payout.percent - 10) < 1e-10);
  assert.equal(payout.periodText, "+21%");
});

test("identifies multiple genuine rates and a nonconventional schedule with no real rate", () => {
  // Annual growth factors 1.1, 1.2 and 1.5 all explain these same cash flows.
  const row = deal(100, 1, start, start + 3 * year);
  const multiple = withFunding(
    [row],
    [
      { at: start + year, amount: -380 },
      { at: start + 2 * year, amount: 477 },
    ],
  );
  assert.equal(multiple.reason, "ambiguous-return");
  assert.equal(multiple.tier, null);
  // 100*x^2 - 100*x + 30 is positive for every positive growth factor x.
  const none = withFunding(
    [deal(100, -30, start, start + 2 * year)],
    [
      { at: start + year, amount: -100 },
      { at: start + 2 * year, amount: 50 },
    ],
  );
  assert.equal(none.reason, "no-return");
  assert.equal(none.lossExceedsCapital, false);
});

test("funded total losses, absent terminal wealth and extreme positive annualization", () => {
  const funds = [{ at: start + day, amount: 100 }];
  const wiped = withFunding([deal(100, -200)], funds);
  assert.equal(wiped.periodText, "−100%");
  assert.equal(wiped.percent, -100);
  const below = withFunding([deal(100, -201)], funds);
  assert.equal(below.lossExceedsCapital, true);
  assert.equal(below.periodText, null);
  const huge = withFunding(
    [deal(100, 1000, start, start + 2)],
    [{ at: start + 1, amount: 100 }],
  );
  assert.equal(huge.percent, Infinity);
  assert.equal(huge.tier, "commander");
  assert.ok(!huge.text.includes("Infinity"));
});
