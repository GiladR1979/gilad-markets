"use strict";

const byId = (id) => document.getElementById(id);
const ns = "http://www.w3.org/2000/svg";
const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});
const day = 86400;
const pageSize = 10;
let data,
  days = 0,
  page = 0,
  scoped = [],
  visible = [];
let inspection = null,
  activePoint = 0;

const iconPaths = {
  wallet:
    "M3 7.5h16a1.5 1.5 0 0 1 1.5 1.5v10H4.5A1.5 1.5 0 0 1 3 17.5v-11L17 3v4.5 M15.5 12h5v4h-5z",
  layers: "m12 3 9 5-9 5-9-5z M3 12l9 5 9-5 M3 16l9 5 9-5",
  book: "M5 3.5h12.5A1.5 1.5 0 0 1 19 5v15H6.5A2.5 2.5 0 0 1 4 17.5V5a1.5 1.5 0 0 1 1-1.5z M4 17.5A2.5 2.5 0 0 1 6.5 15H19 M8 7h7 M8 10h5",
  search: "M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15z M16 16l5 5",
};
function svg(tag, fields, parent, content) {
  const node = document.createElementNS(ns, tag);
  for (const [key, value] of Object.entries(fields))
    node.setAttribute(key, value);
  if (content !== undefined) node.textContent = content;
  parent.append(node);
  return node;
}
for (const slot of document.querySelectorAll("[data-icon]")) {
  const icon = svg(
    "svg",
    {
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": 1.6,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": true,
    },
    slot,
  );
  svg("path", { d: iconPaths[slot.dataset.icon] }, icon);
}
const signClass = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");
function cash(units) {
  const x = Math.abs(units) / data.unit;
  const value = x > 0 && x < 0.005 ? "<$0.01" : usd.format(x);
  return (units < 0 ? "−" : units > 0 ? "+" : "") + value;
}
function dateAt(
  seconds,
  opts = { month: "short", day: "numeric", year: "numeric" },
) {
  return new Intl.DateTimeFormat("en-US", { ...opts, timeZone: "UTC" }).format(
    seconds * 1000,
  );
}
function span(tag, className, text) {
  const el = document.createElement(tag);
  el.className = className;
  el.textContent = text;
  return el;
}
function setMoney(id, amount) {
  const el = byId(id);
  el.textContent = cash(amount);
  el.classList.remove("pos", "neg");
  if (signClass(amount)) el.classList.add(signClass(amount));
}
const sum = (rows) => rows.reduce((total, r) => total + r.pnl, 0);
function updateSummary() {
  const latest = data.deals.filter((r) => r.method);
  setMoney("total", sum(data.deals));
  setMoney("latest", sum(latest));
  if (data.methodSince === null) byId("latest").textContent = "Pending";
  byId("methodName").textContent = data.tag;
  byId("scope").querySelector('[value="method"]').textContent = data.tag;
  byId("methodCount").textContent =
    latest.length + (latest.length === 1 ? " deal" : " deals");
  byId("count").textContent = data.deals.length.toLocaleString("en-US");
  if (data.deals.length)
    byId("firstClose").textContent =
      "First close · " + dateAt(data.deals[data.deals.length - 1].closed);
  byId("updated").textContent =
    "Snapshot · " +
    dateAt(data.checked, { dateStyle: "medium", timeStyle: "short" }) +
    " UTC";
  byId("methodNotes").textContent =
    data.methodSince === null
      ? data.tag +
        " (" +
        data.commit.slice(0, 7) +
        ") has no matching deployment in the bot ledger yet."
      : data.tag +
        " (" +
        data.commit.slice(0, 7) +
        ") is the newest numbered release tag. Its " +
        data.method.toLowerCase() +
        " method was activated on " +
        dateAt(data.methodSince, { dateStyle: "medium", timeStyle: "short" }) +
        " UTC. Every outcome’s lifetime purchases must match confirmed bot buys originating from pairs opened under this release, within the API’s displayed share precision. “Ledger-matched bot” also includes earlier trades that reconcile to this execution ledger. Other closed markets appear in account history.";
}

function renderOutcomes(rows) {
  const won = rows.filter((r) => r.pnl > 0).length;
  const lost = rows.filter((r) => r.pnl < 0).length;
  const win = rows.length ? (100 * won) / rows.length : 0;
  const lossEnd = rows.length ? (100 * (lost + won)) / rows.length : 0;
  byId("outcomePeriod").textContent = days
    ? "Past " + days + " days"
    : "All time";
  byId("winRate").textContent = rows.length ? Math.round(win) + "%" : "—";
  byId("wins").textContent = won;
  byId("losses").textContent = lost;
  byId("flats").textContent = rows.length - lost - won;
  byId("ring").style.background = rows.length
    ? `conic-gradient(#32745a 0 ${win}%, #c48267 ${win}% ${lossEnd}%, #c4caba ${lossEnd}% 100%)`
    : "#dfe4da";
}

function draw() {
  if (!data) return;
  const plot = byId("plot");
  byId("tip").hidden = true;
  inspection = null;
  const end = data.checked;
  const start = days
    ? end - days * day
    : scoped.length
      ? scoped[scoped.length - 1].closed - 1
      : end - day;
  const rows = scoped.filter((r) => r.closed >= start);
  renderOutcomes(rows);
  const total = sum(rows);
  byId("windowResult").textContent = "Period P&L  " + cash(total);
  const w = Math.round(plot.getBoundingClientRect().width),
    h = Math.round(plot.getBoundingClientRect().height);
  if (w < 1 || h < 1) return;
  plot.replaceChildren();
  plot.setAttribute("viewBox", `0 0 ${w} ${h}`);
  plot.setAttribute(
    "aria-label",
    `Cumulative profit. ${rows.length} closed deals, period P&L ${cash(total)}. Use arrow keys to inspect closing dates.`,
  );
  if (!rows.length) {
    svg(
      "text",
      { x: w / 2, y: h / 2, "text-anchor": "middle", class: "empty-chart" },
      plot,
      "No closed deals in this period",
    );
    return;
  }
  const left = 46,
    right = w - 9,
    top = 12,
    bottom = h - 27;
  const points = [{ t: start, v: 0 }];
  for (const r of [...rows].reverse()) {
    const last = points[points.length - 1];
    if (last.t === r.closed) last.v += r.pnl;
    else points.push({ t: r.closed, v: last.v + r.pnl });
  }
  const values = points.map((p) => p.v / data.unit);
  const min = Math.min(0, ...values),
    max = Math.max(0, ...values);
  const rough = Math.max((max - min) / 4, 0.01);
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].find((n) => n >= rough / power) * power;
  const low = min === max ? -step : Math.floor(min / step) * step;
  const high = min === max ? step : Math.ceil(max / step) * step;
  const x = (t) =>
    left + ((right - left) * (t - start)) / Math.max(1, end - start);
  const y = (v) =>
    bottom - ((bottom - top) * (v / data.unit - low)) / (high - low);
  const defs = svg("defs", {}, plot),
    grad = svg(
      "linearGradient",
      {
        id: "wash",
        x1: 0,
        y1: 0,
        x2: 0,
        y2: 1,
        gradientUnits: "objectBoundingBox",
      },
      defs,
    );
  svg(
    "stop",
    { offset: "0%", "stop-color": "#62a280", "stop-opacity": 0.23 },
    grad,
  );
  svg(
    "stop",
    { offset: "100%", "stop-color": "#62a280", "stop-opacity": 0.025 },
    grad,
  );
  for (let v = low, i = 0; v <= high + step / 100 && i < 12; v += step, i++) {
    const py = y(v * data.unit),
      label = Number(Math.abs(v).toFixed(2));
    svg(
      "line",
      {
        x1: left,
        x2: right,
        y1: py,
        y2: py,
        stroke: Math.abs(v) < 1e-8 ? "#aebead" : "#dce3d8",
        "stroke-dasharray": "3 4",
      },
      plot,
    );
    svg(
      "text",
      { x: left - 8, y: py + 4, "text-anchor": "end" },
      plot,
      (v < -1e-8 ? "−" : "") + "$" + label,
    );
  }
  const maxTicks = w < 400 ? 3 : 5;
  const duration = end - start;
  const intraday = duration < day;
  const ticks = intraday
    ? maxTicks
    : Math.min(maxTicks, Math.floor(duration / day) + 1);
  for (let i = 0; i < ticks; i++) {
    const t = start + (i * (end - start)) / (ticks - 1);
    svg(
      "text",
      {
        x: x(t),
        y: h - 4,
        "text-anchor": i === 0 ? "start" : i === ticks - 1 ? "end" : "middle",
      },
      plot,
      dateAt(
        t,
        intraday
          ? { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }
          : { month: "short", day: "numeric" },
      ),
    );
  }
  const path =
    points
      .map((p, i) =>
        i === 0 ? `M${x(p.t)} ${y(p.v)}` : `H${x(p.t)}V${y(p.v)}`,
      )
      .join(" ") +
    "H" +
    right;
  svg("path", { d: path + `V${bottom}H${left}Z`, fill: "url(#wash)" }, plot);
  svg(
    "path",
    {
      d: path,
      fill: "none",
      stroke: "#347457",
      "stroke-width": 2.4,
      "stroke-linejoin": "round",
    },
    plot,
  );
  svg(
    "circle",
    {
      cx: right,
      cy: y(total),
      r: 4.2,
      fill: "#2e6b51",
      stroke: "white",
      "stroke-width": 1.8,
    },
    plot,
  );
  const marker = svg("g", { visibility: "hidden" }, plot);
  const hair = svg(
    "line",
    { y1: top, y2: bottom, stroke: "#6c8978", "stroke-dasharray": "3 3" },
    marker,
  );
  const ball = svg(
    "circle",
    { r: 4, fill: "#244e40", stroke: "white", "stroke-width": 1.5 },
    marker,
  );
  inspection = { points, x, y, w, start, end, left, right, marker, hair, ball };
  activePoint = points.length - 1;
}
function inspect(index) {
  if (!inspection) return;
  const { points, x, y, w, marker, hair, ball } = inspection;
  activePoint = Math.max(0, Math.min(index, points.length - 1));
  const p = points[activePoint],
    px = x(p.t),
    py = y(p.v),
    tip = byId("tip");
  marker.setAttribute("visibility", "visible");
  hair.setAttribute("x1", px);
  hair.setAttribute("x2", px);
  ball.setAttribute("cx", px);
  ball.setAttribute("cy", py);
  tip.textContent =
    dateAt(p.t, { month: "short", day: "numeric" }) + " · " + cash(p.v);
  tip.hidden = false;
  tip.style.left =
    Math.max(0, Math.min(w - tip.offsetWidth, px - tip.offsetWidth / 2)) + "px";
  tip.style.top = Math.max(0, py - 33) + "px";
}
function hideTip() {
  if (inspection) inspection.marker.setAttribute("visibility", "hidden");
  byId("tip").hidden = true;
}
byId("plot").addEventListener("pointermove", (e) => {
  if (!inspection) return;
  const { points, x } = inspection,
    px = e.clientX - byId("plot").getBoundingClientRect().left;
  let i = 0;
  while (i + 1 < points.length && x(points[i + 1].t) <= px) i++;
  inspect(i);
});
byId("plot").addEventListener("pointerleave", hideTip);
byId("plot").addEventListener("focus", () => inspect(activePoint));
byId("plot").addEventListener("blur", hideTip);
byId("plot").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
  e.preventDefault();
  inspect(activePoint + (e.key === "ArrowRight" ? 1 : -1));
});

function renderList() {
  const items = visible.slice(page * pageSize, (page + 1) * pageSize);
  const list = byId("rows");
  list.replaceChildren();
  list.start = page * pageSize + 1;
  for (const r of items) {
    const li = span("li", "deal-grid deal", "");
    const market = span("div", "market", "");
    market.append(
      span("div", "market-name", r.market),
      span(
        "div",
        "deal-meta",
        r.legs +
          (r.legs === 1 ? " outcome" : " outcomes") +
          (r.dust ? " · Dust closed" : ""),
      ),
    );
    const time = span("time", "", dateAt(r.closed));
    time.dateTime = new Date(r.closed * 1000).toISOString();
    time.title = time.dateTime;
    const method = span("div", "method-cell", "");
    method.append(
      span(
        "span",
        r.method ? "method-tag" : "",
        r.method ? data.tag : r.verified ? "Bot ledger" : "Account history",
      ),
    );
    li.append(
      market,
      time,
      method,
      span("span", "pnl " + signClass(r.pnl), cash(r.pnl)),
    );
    list.append(li);
  }
  if (!items.length)
    list.append(span("li", "empty", "No closed deals match these filters."));
  byId("prev").disabled = page === 0;
  byId("next").disabled = (page + 1) * pageSize >= visible.length;
  byId("pageInfo").textContent = items.length
    ? `${page * pageSize + 1}–${page * pageSize + items.length} of ${visible.length} deals`
    : "0 deals";
}
function filterList() {
  if (!data) return;
  const term = byId("search").value.toLocaleLowerCase().trim(),
    result = byId("result").value;
  visible = scoped.filter(
    (r) =>
      r.market.toLocaleLowerCase().includes(term) &&
      (result === "all" ||
        (result === "win" && r.pnl > 0) ||
        (result === "loss" && r.pnl < 0) ||
        (result === "flat" && r.pnl === 0)),
  );
  page = 0;
  renderList();
}
function changeScope() {
  if (!data) return;
  const field = byId("scope").value;
  scoped = field === "all" ? data.deals : data.deals.filter((r) => r[field]);
  byId("ledgerDesc").textContent =
    `${scoped.length} markets · All dates · Newest first`;
  draw();
  filterList();
}
byId("scope").addEventListener("change", changeScope);
byId("search").addEventListener("input", filterList);
byId("result").addEventListener("change", filterList);
byId("prev").addEventListener("click", () => {
  page--;
  renderList();
});
byId("next").addEventListener("click", () => {
  page++;
  renderList();
});
for (const button of document.querySelectorAll("[data-days]"))
  button.addEventListener("click", () => {
    days = Number(button.dataset.days);
    for (const other of document.querySelectorAll("[data-days]"))
      other.setAttribute("aria-pressed", String(button === other));
    draw();
  });
byId("about").addEventListener("click", () => byId("notes").showModal());
byId("close").addEventListener("click", () => byId("notes").close());
byId("notes").addEventListener("click", (e) => {
  if (e.target === byId("notes")) {
    const r = byId("notes").getBoundingClientRect();
    if (
      e.clientX < r.left ||
      e.clientX > r.right ||
      e.clientY < r.top ||
      e.clientY > r.bottom
    )
      byId("notes").close();
  }
});
new ResizeObserver(draw).observe(byId("plotWrap"));
function notice(text) {
  byId("notice").textContent = text;
  byId("notice").hidden = !text;
}
async function loadSnapshot() {
  try {
    const response = await fetch("deals.json", { cache: "no-cache" });
    if (!response.ok) throw Error("Snapshot unavailable");
    const next = await response.json();
    if (
      next.version !== 1 ||
      next.unit !== 10000 ||
      !Array.isArray(next.deals) ||
      !Number.isSafeInteger(next.checked) ||
      !next.deals.every(
        (r) =>
          Number.isSafeInteger(r.pnl) &&
          Number.isSafeInteger(r.closed) &&
          typeof r.market === "string",
      )
    )
      throw Error("Invalid snapshot");
    if (!data || data.checked !== next.checked) {
      data = next;
      updateSummary();
      changeScope();
    }
    notice(
      Date.now() / 1000 - data.checked > 8 * 3600
        ? "The publisher’s last snapshot is over eight hours old. The figures below are from the time shown in the footer."
        : "",
    );
  } catch {
    notice(
      data
        ? "Refresh unavailable. Showing the last loaded snapshot."
        : "The performance snapshot is unavailable. Please try again shortly.",
    );
    if (!data) {
      byId("pageInfo").textContent = "Data unavailable";
      byId("updated").textContent = "Snapshot unavailable";
    }
  }
}
loadSnapshot();
setInterval(
  () => {
    if (!document.hidden) loadSnapshot();
  },
  5 * 60 * 1000,
);
