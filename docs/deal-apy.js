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

  // s is seconds remaining to close. Solve sum(c * exp(s / duration * x)) = 0,
  // where x is the logarithm of the period growth factor.
  function evaluate(terms, x, duration) {
    const anchor = x > 0 ? terms.at(-1).s : terms[0].s;
    // Near zero, small gains can disappear if exp(x) rounds to one.
    const baseline =
      x !== 0 && Math.abs(x) < 0.001 ? evaluate(terms, 0, duration) : null;
    let sum = 0,
      correction = 0,
      size = 0;
    for (const { s, c } of terms) {
      const exponent = ((s - anchor) / duration) * x;
      const value = c * (baseline ? Math.expm1(exponent) : Math.exp(exponent));
      const next = sum + value;
      correction +=
        Math.abs(sum) >= Math.abs(value)
          ? sum - next + value
          : value - next + sum;
      sum = next;
      size += Math.abs(value);
    }
    return {
      value: (baseline?.value ?? 0) + (sum + correction),
      size: baseline?.size ?? size,
    };
  }

  function bisect(terms, duration, lo, hi, signLo) {
    for (let i = 0; i < 160; i++) {
      const mid = lo + (hi - lo) / 2;
      if (mid === lo || mid === hi) break;
      const sign = Math.sign(evaluate(terms, mid, duration).value);
      if (!sign) return mid;
      if (sign === signLo) lo = mid;
      else hi = mid;
    }
    return lo + (hi - lo) / 2;
  }

  function growthRoots(terms, duration, unique, depth = 0) {
    const first = terms[0],
      last = terms.at(-1);
    const pivot = terms.findIndex((p) => Math.sign(p.c) !== Math.sign(first.c));
    if (pivot < 0) return [];
    // Outside these bounds the end coefficient dominates all the others.
    const total = terms.reduce((n, p) => n + Math.abs(p.c), 0);
    const lo =
      (-duration * Math.log((2 * total) / Math.abs(first.c))) /
      (terms[1].s - first.s);
    const hi =
      (duration * Math.log((2 * total) / Math.abs(last.c))) /
      (last.s - terms.at(-2).s);
    if (
      unique ||
      terms.slice(pivot).every((p) => Math.sign(p.c) === Math.sign(last.c))
    ) {
      if (evaluate(terms, 0, duration).value === 0) return [0];
      return [bisect(terms, duration, lo, hi, Math.sign(first.c))];
    }

    // Dividing by the positive pivot exponential preserves roots. The derivative
    // removes the pivot and a sign change. Its roots bound monotone intervals.
    if (depth >= 24) return null;
    const turn = terms[pivot].s;
    const derivative = terms
      .filter((p) => p.s !== turn)
      .map(({ s, c }) => ({
        s,
        c: (c / total) * ((s - turn) / duration),
      }));
    const critical = growthRoots(derivative, duration, false, depth + 1);
    if (!critical) return null;
    const roots = [];
    let left = lo,
      leftSign = Math.sign(first.c);
    for (const right of [...critical.filter((x) => lo < x && x < hi), hi]) {
      const at = evaluate(terms, right, duration);
      const sign = Math.sign(at.value);
      // A near-tangent root cannot be distinguished from two close roots at
      // machine precision. Never display an arbitrarily chosen rate.
      if (right < hi && Math.abs(at.value) <= at.size * 8 * Number.EPSILON)
        return null;
      if (leftSign !== sign)
        roots.push(bisect(terms, duration, left, right, leftSign));
      left = right;
      leftSign = sign;
    }
    return roots;
  }

  function calculate(rows, externalFlows) {
    const closed = rows.filter((r) => r.markedAt == null);
    const result = {
      percent: null,
      text: "—",
      periodText: null,
      flowCount: null,
      netFlows: null,
      lossExceedsCapital: false,
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
    if (
      !Array.isArray(externalFlows) ||
      !externalFlows.every(
        (f) =>
          Number.isSafeInteger(f?.at) &&
          f.at > 0 &&
          Number.isSafeInteger(f.amount),
      )
    )
      return { ...result, reason: "missing-flows" };
    const byTime = new Map();
    let netFlows = 0,
      flowCount = 0;
    for (const { at, amount } of externalFlows) {
      if (at <= equityAt || at > end || !amount) continue;
      const atTotal = (byTime.get(at) ?? 0) + amount;
      netFlows += amount;
      if (!Number.isSafeInteger(netFlows) || !Number.isSafeInteger(atTotal))
        return { ...result, reason: "invalid-return" };
      byTime.set(at, atTotal);
      flowCount++;
    }
    Object.assign(result, { flowCount, netFlows });
    if (equity <= 0) return { ...result, reason: "invalid-return" };
    const ending = equity + netFlows + pnl;
    if (
      !Number.isSafeInteger(ending) ||
      !Number.isSafeInteger(equity + netFlows)
    )
      return { ...result, reason: "invalid-return" };
    result.lossExceedsCapital = ending < 0;
    const intermediate = [...byTime]
      .filter(([at, amount]) => at < end && amount)
      .sort(([a], [b]) => a - b);
    let periodPercent, periodLog;
    if (!intermediate.length) {
      const periodReturn = pnl / equity;
      periodPercent = 100 * periodReturn;
      result.periodText = (pnl > 0 ? "+" : "") + formatPercent(periodPercent);
      if (!elapsed) return { ...result, reason: "no-duration" };
      if (ending < 0 || pnl < -equity)
        return { ...result, reason: "invalid-return" };
      periodLog = Math.log1p(periodReturn);
    } else {
      if (ending < 0) return { ...result, reason: "no-return" };
      const terms = [{ s: elapsed, c: equity }];
      let invested = equity,
        leastInvested = equity;
      for (const [at, amount] of intermediate) {
        terms.push({ s: end - at, c: amount });
        invested += amount;
        if (!Number.isSafeInteger(invested))
          return { ...result, reason: "invalid-return" };
        leastInvested = Math.min(leastInvested, invested);
      }
      // A transfer at the valuation instant changes ending wealth one-for-one.
      const receipt = ending - (byTime.get(end) ?? 0);
      if (!Number.isSafeInteger(receipt))
        return { ...result, reason: "invalid-return" };
      if (receipt) terms.push({ s: 0, c: -receipt });
      terms.reverse();
      if (receipt === 0 && terms.every((p) => p.c > 0)) {
        periodLog = -Infinity;
      } else {
        // Positive outstanding funding implies uniqueness on the gain side.
        // For losses reverse the timeline: funding plus P&L must stay positive.
        const unique = leastInvested + Math.min(0, pnl) > 0;
        const roots = growthRoots(terms, elapsed, unique);
        if (!roots) return { ...result, reason: "unresolved-return" };
        if (roots.length !== 1)
          return {
            ...result,
            reason: roots.length ? "ambiguous-return" : "no-return",
          };
        periodLog = roots[0];
      }
      periodPercent = 100 * Math.expm1(periodLog);
      result.periodText =
        (periodPercent > 0 ? "+" : "") +
        percentText(periodPercent, periodLog, periodLog === -Infinity);
    }
    const annualLogGrowth = periodLog * (year / elapsed);
    let percent = periodPercent;
    if (elapsed !== year || intermediate.length) {
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
      text: percentText(percent, annualLogGrowth, periodLog === -Infinity),
      tier: tierFor(percent),
    };
  }

  return { calculate, tierFor };
})();
