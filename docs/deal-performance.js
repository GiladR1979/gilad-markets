"use strict";

function performanceSeries(deals, since, through) {
  const changes = new Map();
  const contributing = [];
  for (const deal of deals) {
    const end = deal.markedAt ?? deal.closed;
    const events = [];
    if (
      Number.isSafeInteger(deal.crashAt) &&
      Number.isSafeInteger(deal.crashPnl) &&
      deal.crashAt < end
    ) {
      events.push([deal.crashAt, deal.crashPnl]);
      if (deal.pnl !== deal.crashPnl)
        events.push([end, deal.pnl - deal.crashPnl]);
    } else {
      events.push([end, deal.pnl]);
    }
    let counted = false;
    for (const [at, change] of events) {
      if (at < since || at > through) continue;
      changes.set(at, (changes.get(at) ?? 0) + change);
      counted = true;
    }
    if (counted) contributing.push(deal);
  }
  let total = 0;
  const points = [...changes]
    .sort(([a], [b]) => a - b)
    .map(([t, change]) => {
      total += change;
      return { t, v: total };
    });
  return { points, total, contributing };
}

if (typeof module !== "undefined") module.exports = performanceSeries;
