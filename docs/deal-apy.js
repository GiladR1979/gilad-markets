"use strict";

globalThis.dealApy = (() => {
  const year = 365 * 86400;
  const portraitCutoffs = [50, 100, 300, 1000, 10000];
  const percentNumber = new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 2,
  });

  function tierFor(percent) {
    if (typeof percent !== "number" || Number.isNaN(percent)) return null;
    if (percent > 10000) return "commander";
    if (percent > 1000) return "tokyo";
    if (percent >= 300) return "programmer";
    if (percent >= 100) return "sweatpants";
    if (percent >= 50) return "anime";
    if (percent >= 0) return "unhoused";
    return "skeleton";
  }

  function formatPercent(percent) {
    const magnitude = Math.abs(percent);
    if (magnitude >= 1e6 || (magnitude > 0 && magnitude < 0.01))
      return percent.toExponential(2).replace("-", "−") + "%";
    return percentNumber.format(percent).replace("-", "−") + "%";
  }

  function percentText(percent, annualLogGrowth, totalLoss) {
    if (percent === Infinity) {
      // The exponent remains finite even for a very profitable one-second trade.
      const log10Percent = annualLogGrowth / Math.LN10 + 2;
      let power = Math.floor(log10Percent);
      let coefficient = Number((10 ** (log10Percent - power)).toPrecision(3));
      if (coefficient === 10) {
        coefficient = 1;
        power++;
      }
      return coefficient + "e+" + power + "%";
    }
    if (percent <= -99.995 && !totalLoss) return "≈−100%";
    return formatPercent(percent);
  }

  function calculate(rows) {
    const closed = rows.filter((r) => r.markedAt == null);
    const result = {
      percent: null,
      text: "—",
      periodText: null,
      tier: null,
      reason: null,
      count: closed.length,
      provisionalCount: rows.length - closed.length,
      missingCount: 0,
      equity: null,
      equityAt: null,
      pnl: null,
      start: null,
      firstOpened: null,
      end: null,
      days: null,
    };
    if (!closed.length) return { ...result, reason: "no-closed-deals" };

    let pnl = 0,
      firstOpened = Infinity,
      end = -Infinity;
    for (const row of closed) {
      const history = row.history;
      if (
        !Number.isSafeInteger(history?.opened) ||
        history.opened <= 0 ||
        !Number.isSafeInteger(row.closed) ||
        row.closed < history.opened ||
        !Number.isSafeInteger(row.pnl)
      ) {
        result.missingCount++;
        continue;
      }
      pnl += row.pnl;
      firstOpened = Math.min(firstOpened, history.opened);
      end = Math.max(end, row.closed);
    }
    if (result.missingCount) return { ...result, reason: "missing-history" };
    if (!Number.isSafeInteger(pnl))
      return { ...result, reason: "invalid-return" };
    Object.assign(result, {
      pnl,
      firstOpened,
      start: firstOpened,
      end,
      days: (end - firstOpened) / 86400,
    });

    // Deals opened together share one bankroll. Use the latest verified
    // pre-entry observation, requiring agreement between observations tied in time.
    let equityAt = 0,
      equity = null;
    for (const row of closed) {
      if (row.history.opened !== firstOpened) continue;
      const point = row.history.openingEquity;
      if (
        !Number.isSafeInteger(point?.at) ||
        point.at <= 0 ||
        point.at > firstOpened ||
        !Number.isSafeInteger(point.value)
      )
        continue;
      if (point.at > equityAt) {
        equityAt = point.at;
        equity = point.value;
      } else if (point.at === equityAt && point.value !== equity) {
        equity = null;
      }
    }
    if (equity === null) return { ...result, reason: "missing-equity" };
    const elapsed = end - equityAt;
    Object.assign(result, {
      equity,
      equityAt,
      start: equityAt,
      days: elapsed / 86400,
    });
    if (equity <= 0) return { ...result, reason: "invalid-return" };
    const periodReturn = pnl / equity;
    result.periodText =
      (pnl > 0 ? "+" : "") + formatPercent(periodReturn * 100);
    if (!elapsed) return { ...result, reason: "no-duration" };
    if (pnl < -equity) return { ...result, reason: "invalid-return" };
    const annualLogGrowth = Math.log1p(periodReturn) * (year / elapsed);
    let percent = 100 * periodReturn;
    if (elapsed !== year) {
      percent = 100 * Math.expm1(annualLogGrowth);
      // Logarithm and exponent rounding can move an exact cutoff by a few ULPs.
      const cutoff = portraitCutoffs.find(
        (value) => Math.abs(value - percent) <= 8 * Number.EPSILON * value,
      );
      percent = cutoff ?? percent;
    }
    return {
      ...result,
      percent,
      text: percentText(percent, annualLogGrowth, pnl === -equity),
      tier: tierFor(percent),
    };
  }

  return { calculate, tierFor };
})();
