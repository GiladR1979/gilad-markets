"use strict";

const apyPortraits = {
  commander:
    "Gilad in a suit commanding a Japanese spaceship alongside robot assistants, whisky and bundles of yen on the table.",
  tokyo: "Gilad exploring the streets of Tokyo in sunglasses.",
  programmer: "Gilad at his programming desk, listening to music on AirPods.",
  sweatpants:
    "Gilad working at his computer in worn clothes and training pants.",
  anime: "Gilad sitting in a dark room, watching anime on television.",
  unhoused: "Gilad living on the street with his belongings.",
  skeleton:
    "A skeleton sitting on a public bench with a sign reading “Will code for Hamburger”.",
};

function apyDuration(days) {
  if (days >= 1)
    return new Intl.NumberFormat(undefined, {
      style: "unit",
      unit: "day",
      unitDisplay: "long",
      maximumFractionDigits: 1,
    }).format(days);
  const hours = days * 24;
  if (hours < 1)
    return hours * 60 < 1
      ? "under a minute"
      : new Intl.NumberFormat(undefined, {
          style: "unit",
          unit: "minute",
          unitDisplay: "long",
          maximumFractionDigits: 0,
        }).format(hours * 60);
  return new Intl.NumberFormat(undefined, {
    style: "unit",
    unit: "hour",
    unitDisplay: "long",
    maximumFractionDigits: 1,
  }).format(hours);
}

function renderApyCard(estimate) {
  const { text, tier, reason, count, days, missingCount, provisionalCount } =
    estimate;
  const card = byId("apy"),
    image = byId("apy-image"),
    value = byId("apy-value");
  const funded = estimate.flowCount > 0;
  card.dataset.tier = tier ?? "unavailable";
  byId("apy-heading").textContent = funded
    ? "Money-weighted bankroll APY"
    : "Estimated bankroll APY";
  if (tier && image.dataset.tier !== tier) {
    image.src = "apy/" + tier + ".webp";
    image.alt = apyPortraits[tier];
    image.dataset.tier = tier;
  }
  image.hidden = !tier;
  value.textContent = text;
  value.style.setProperty(
    "--apy-value-size",
    Math.min(18, 125 / text.length) + "cqi",
  );

  const sample = byId("apy-sample");
  const deals =
    count.toLocaleString() + (count === 1 ? " closed deal" : " closed deals");
  if (reason === "no-closed-deals") {
    sample.textContent = provisionalCount
      ? "Only open positions match these filters. APY uses closed deals."
      : "No closed deals match these filters.";
  } else if (reason === "missing-history") {
    sample.textContent =
      "APY unavailable. Entry or closing records are missing for " +
      missingCount.toLocaleString() +
      " of " +
      deals +
      ".";
  } else if (reason === "missing-equity") {
    sample.textContent =
      estimate.start != null
        ? "Starting account equity unavailable for " +
          localCloseTime.format(estimate.start * 1000) +
          "."
        : "Starting account equity unavailable for these deals.";
  } else if (reason === "no-duration") {
    sample.textContent =
      "APY unavailable. The equity observation and final close have the same timestamp.";
  } else if (reason === "missing-flows") {
    sample.textContent =
      "APY unavailable. External funding could not be verified for the full period.";
  } else if (reason && estimate.lossExceedsCapital) {
    sample.textContent =
      estimate.pnl < 0
        ? "Losses exceed the opening bankroll plus net funding, so this return cannot be compounded."
        : "The selected deals and transfers imply a negative ending balance. APY is unavailable.";
  } else if (reason === "ambiguous-return") {
    sample.textContent =
      "The transfer dates and ending balance imply multiple annualized rates. There is no unique APY.";
  } else if (reason === "no-return") {
    sample.textContent =
      "APY unavailable. No annualized rate fits the dated transfers and estimated ending balance.";
  } else if (reason === "unresolved-return") {
    sample.textContent =
      "APY unavailable. A reliable annualized rate could not be established for these transfers.";
  } else if (reason) {
    sample.textContent =
      "APY unavailable. The combined return cannot be compounded.";
  } else {
    sample.textContent =
      (funded ? "Money-weighted return " : "Period return ") +
      estimate.periodText +
      " over " +
      apyDuration(days) +
      " · " +
      deals;
  }
  const inputs = byId("apy-inputs");
  inputs.hidden = estimate.start == null && estimate.pnl == null;
  if (!inputs.hidden) {
    byId("apy-equity").textContent =
      estimate.equity == null
        ? "Unavailable"
        : usd.format(estimate.equity / data.unit);
    const observation = byId("apy-equity-at-row");
    observation.hidden = estimate.equity == null || estimate.equityAt == null;
    if (!observation.hidden) {
      const instant = new Date(estimate.equityAt * 1000);
      const time = byId("apy-equity-at");
      time.dateTime = instant.toISOString();
      time.textContent = localCloseTime.format(instant);
      time.title = preciseCloseTime.format(instant);
    }
    byId("apy-pnl").textContent =
      estimate.pnl == null ? "Unavailable" : cash(estimate.pnl);
    byId("apy-funding").textContent =
      estimate.flowCount == null || estimate.netFlows == null
        ? "Unverified"
        : cash(estimate.netFlows) +
          " · " +
          estimate.flowCount.toLocaleString() +
          (estimate.flowCount === 1 ? " transfer" : " transfers");
    byId("apy-return-label").textContent = funded
      ? "Money-weighted return"
      : "Period return";
    byId("apy-period-return-row").hidden = estimate.periodText == null;
    byId("apy-period-return").textContent = estimate.periodText ?? "";
    byId("apy-period-label").textContent =
      estimate.equityAt == null ? "Deal dates" : "Observed period";
    byId("apy-period").textContent =
      estimate.start == null || estimate.end == null
        ? "Unavailable"
        : localCloseTime.formatRange(
            estimate.start * 1000,
            estimate.end * 1000,
          );
  }
}
