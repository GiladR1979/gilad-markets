"use strict";

const moodLabels = {
  smooth: "Smooth",
  troubling: "Troubling",
  "nerve-wracking": "Nerve-wracking",
  losing: "Losing",
  unrated: "Unrated",
};

function axisMoney(units) {
  const dollars = Math.abs(units) / data.unit;
  return (
    (units < 0 ? "−" : "") +
    "$" +
    Number(dollars.toFixed(4)).toLocaleString(undefined, {
      maximumFractionDigits: 4,
    })
  );
}

function dealTime(seconds) {
  const date = new Date(seconds * 1000);
  const time = span("time", "", localCloseTime.format(date));
  time.dateTime = date.toISOString();
  time.title = preciseCloseTime.format(date);
  return time;
}

function closeReason(deal) {
  if (deal.closeReason) return deal.closeReason;
  if (deal.settled) return "settlement";
  return deal.dust ? "dust" : "exit";
}

function plotScale(samples) {
  let low = 0,
    high = 0;
  for (const [, value] of samples) {
    low = Math.min(low, value);
    high = Math.max(high, value);
  }
  const rough = Math.max(1, (high - low) / 3);
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 5, 10].find((n) => n * power >= rough) * power;
  low = Math.floor(low / step) * step;
  high = Math.ceil(high / step) * step;
  if (low === high) high = step;
  const ticks = [];
  for (let v = low; v <= high; v += step) ticks.push(v);
  return { low, high, ticks };
}

function dealPlot(deal, history) {
  const crashed = deal.crashAt != null;
  const end = deal.crashAt ?? endpointTime(deal);
  const start = history.opened ?? end;
  const duration = end - start;
  let segments = history.segments;
  let endValue = deal.pnl;
  if (crashed) {
    endValue = deal.crashPnl ?? null;
    segments = [];
    for (const segment of history.segments) {
      const visible = segment.filter(([t]) => t <= duration);
      if (!visible.length) continue;
      const last = visible.at(-1);
      if (last[0] === duration) {
        endValue ??= last[1];
        visible[visible.length - 1] = [duration, endValue];
      } else if (endValue != null && segment.at(-1)[0] >= duration - 1) {
        // A compressed segment spans the crash, or ends one second before exit.
        visible.push([duration, endValue]);
      }
      segments.push(visible);
    }
  }
  const samples = segments.flat().filter(([t]) => t !== duration);
  samples.push([duration, endValue]);
  const { low, high, ticks } = plotScale(samples.filter(([, v]) => v != null));
  const endLabel = crashed ? "At crash" : deal.priceRule ? "Marked" : "Closed";
  const endText =
    endLabel + " · " + (endValue == null ? "P&L unavailable" : cash(endValue));
  const width = 400,
    height = 100,
    inset = 7;
  const x = (t) =>
    duration ? inset + ((width - 2 * inset) * t) / duration : width - inset;
  const y = (v) => inset + ((height - 2 * inset) * (high - v)) / (high - low);
  const frame = span("div", "deal-plot", "");
  const scale = span("div", "deal-axis", "");
  scale.setAttribute("aria-hidden", "true");
  scale.dataset.width = ticks
    .map(axisMoney)
    .sort((a, b) => b.length - a.length)[0];
  const field = span("div", "deal-field", "");
  frame.append(scale, field);
  const chart = svg(
    "svg",
    {
      viewBox: "0 0 " + width + " " + height,
      preserveAspectRatio: "none",
      tabindex: 0,
      role: "img",
      "aria-label":
        deal.market +
        ". " +
        (segments.length
          ? "Estimated open P&L. "
          : "Only the endpoint is available. ") +
        endText +
        ". Use left and right arrows to inspect samples.",
    },
    field,
  );
  for (const v of ticks) {
    const label = span("span", "", axisMoney(v));
    label.style.top = y(v) + "%";
    scale.append(label);
    svg(
      "line",
      {
        class: v === 0 ? "grid-zero" : "grid-line",
        x1: 0,
        x2: width,
        y1: y(v),
        y2: y(v),
      },
      chart,
    );
  }
  for (const segment of segments) {
    if (!segment.length) continue;
    const first = segment[0];
    if (segment.length === 1) {
      svg(
        "circle",
        { class: "sample-dot", cx: x(first[0]), cy: y(first[1]), r: 2 },
        chart,
      );
      continue;
    }
    const path = segment
      .map(
        ([t, v], i) =>
          (i ? "L" : "M") + x(t).toFixed(2) + "," + y(v).toFixed(2),
      )
      .join(" ");
    svg(
      "path",
      {
        class: "sample-area",
        d: path + " V" + y(0) + " H" + x(first[0]) + " Z",
      },
      chart,
    );
    svg("path", { class: "sample-line", d: path }, chart);
  }
  const endX = x(duration);
  if (crashed) {
    const line = svg(
      "line",
      {
        class: "crash-guide",
        x1: endX,
        x2: endX,
        y1: inset,
        y2: height - inset,
      },
      chart,
    );
    svg(
      "title",
      {},
      line,
      "99% drop first observed · " +
        preciseCloseTime.format(deal.crashAt * 1000),
    );
  }
  if (endValue != null) {
    svg(
      "path",
      {
        class: "close-diamond",
        d: "M" + endX + "," + (y(endValue) - 5) + " l5,5 -5,5 -5,-5 Z",
      },
      chart,
    );
  }
  const cursor = svg(
    "circle",
    { class: "sample-cursor", r: 4, visibility: "hidden" },
    chart,
  );
  const readout = span("output", "sample-readout", "");
  readout.hidden = true;
  field.append(readout);
  let active = samples.length - 1;
  function inspect(index) {
    active = Math.max(0, Math.min(index, samples.length - 1));
    const [t, value] = samples[active];
    cursor.setAttribute("cx", x(t));
    if (value != null) cursor.setAttribute("cy", y(value));
    cursor.setAttribute("visibility", value == null ? "hidden" : "visible");
    const atEnd = active === samples.length - 1;
    const formatter = crashed && atEnd ? preciseCloseTime : localCloseTime;
    readout.textContent =
      (atEnd ? endText : "Estimate " + cash(value)) +
      " · " +
      formatter.format((start + t) * 1000);
    readout.hidden = false;
  }
  function nearest(event) {
    const box = chart.getBoundingClientRect();
    const target = (width * (event.clientX - box.left)) / box.width;
    let best = 0;
    for (let i = 1; i < samples.length; i++)
      if (
        Math.abs(x(samples[i][0]) - target) <=
        Math.abs(x(samples[best][0]) - target)
      )
        best = i;
    inspect(best);
  }
  function hide() {
    cursor.setAttribute("visibility", "hidden");
    readout.hidden = true;
  }
  chart.addEventListener("pointermove", nearest);
  chart.addEventListener("pointerdown", nearest);
  chart.addEventListener("pointerleave", (event) => {
    if (event.pointerType !== "touch") hide();
  });
  chart.addEventListener("pointercancel", hide);
  chart.addEventListener("focus", () => inspect(active));
  chart.addEventListener("blur", hide);
  chart.addEventListener("keydown", (event) => {
    if (event.key === "Escape") return hide();
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Home") inspect(0);
    else if (event.key === "End") inspect(samples.length - 1);
    else inspect(active + (event.key === "ArrowRight" ? 1 : -1));
  });
  return frame;
}

function renderDealCard(deal) {
  const h = deal.history ?? {
    coverage: "close",
    segments: [],
    mood: deal.pnl < 0 ? "losing" : "unrated",
  };
  const card = span("li", "deal", "");
  const top = span("div", "deal-top", "");
  const heading = span("div", "deal-heading", "");
  const attribution = deal.method
    ? data.tag
    : deal.verified
      ? "Bot ledger"
      : "Account history";
  heading.append(
    span(
      "div",
      "deal-source",
      attribution +
        " · " +
        deal.legs +
        (deal.legs === 1 ? " outcome" : " outcomes"),
    ),
    span("h3", "market-name", deal.market),
  );
  const result = span("div", "deal-result", "");
  result.append(
    span("strong", "pnl " + signClass(deal.pnl), cash(deal.pnl)),
    span(
      "span",
      "result-label",
      deal.priceRule
        ? "Provisional loss · position open"
        : closeReason(deal) === "settlement"
          ? "Settlement P&L"
          : deal.bookedAt > deal.closed
            ? "Final P&L"
            : "Realized P&L",
    ),
  );
  heading.append(result);
  if (deal.priceRule) {
    const { outcome, entry, mark } = deal.priceRule;
    const cents = new Intl.NumberFormat(undefined, {
      maximumFractionDigits: 5,
    });
    heading.append(
      span(
        "p",
        "deal-mark",
        outcome +
          " · " +
          cents.format(entry * 100) +
          "¢ entry → " +
          cents.format(mark * 100) +
          "¢ now · " +
          ((1 - mark / entry) * 100).toLocaleString(undefined, {
            maximumFractionDigits: 2,
          }) +
          "% drop",
      ),
    );
  }
  const face = span("div", "deal-face", "");
  const photo = document.createElement("img");
  photo.src = "moods/" + h.mood + ".webp";
  photo.width = photo.height = 78;
  photo.alt = "";
  photo.decoding = "async";
  const mood = span("span", "deal-mood", moodLabels[h.mood]);
  mood.setAttribute("aria-label", "Deal mood: " + moodLabels[h.mood]);
  face.append(photo, mood);
  top.append(heading, face);

  const info = span("div", "deal-chart-head", "");
  info.append(
    span(
      "span",
      "plot-name",
      deal.crashAt != null ? "P&L through crash" : "P&L history",
    ),
  );
  const coverage =
    h.coverage === "full"
      ? (deal.crashAt != null ? "Full-deal pullback " : "Max pullback ") +
        h.pullback.toLocaleString(undefined, { maximumFractionDigits: 2 }) +
        "%"
      : h.coverage === "partial"
        ? "Partial history"
        : deal.crashAt != null
          ? deal.crashPnl == null
            ? "Crash time only"
            : "Crash mark only"
          : deal.priceRule
            ? "Current mark only"
            : "Close only";
  const detail = span("span", "plot-detail", coverage);
  detail.title =
    h.coverage === "full"
      ? "Largest drop from a previous high as a share of total dollars invested"
      : h.coverage === "partial"
        ? "Some open-position prices are missing or too sparse to rate the ride."
        : "Open-position prices are unavailable.";
  info.append(detail);
  const dates = span("div", "deal-dates", "");
  const opened = span("div", "", "");
  opened.append(
    span("span", "", "Opened"),
    span(
      "div",
      "",
      h.opened ? localCloseTime.format(h.opened * 1000) : "Unavailable",
    ),
  );
  const closed = span("div", "", "");
  closed.append(
    span(
      "span",
      "",
      deal.priceRule
        ? "Last marked"
        : closeReason(deal) === "settlement"
          ? "Settled"
          : closeReason(deal) === "dust"
            ? "Closed with dust"
            : "Closed",
    ),
    dealTime(endpointTime(deal)),
  );
  dates.append(opened, closed);
  card.append(top, info, dealPlot(deal, h));
  if (deal.crashAt != null) {
    const crossing = span("div", "crash-clock", "");
    crossing.append(
      span("span", "", "99% drop first observed"),
      dealTime(deal.crashAt),
    );
    card.append(crossing);
  }
  card.append(dates);
  if (deal.bookedAt > deal.closed) {
    const booking = span("div", "deal-booking", "");
    booking.append("Last booking ", dealTime(deal.bookedAt));
    card.append(booking);
  }
  return card;
}
