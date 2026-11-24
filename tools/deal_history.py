"""Reconstruct open-position marks from a closed market's public cash flows and prices."""

import bisect
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from itertools import groupby, pairwise
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UNIT = 10_000
GAP = 240
DUST = Decimal("0.01")
QUANTUM = Decimal("0.0001")


def get_json(endpoint, params, *, timeout=14):
    request = Request(
        endpoint + "?" + urlencode(params),
        headers={"User-Agent": "GiladPublicDashboard/1.0"},
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response, parse_float=Decimal)
        except HTTPError as exc:
            exc.close()
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            delay = int(retry_after) if retry_after.isdigit() else 2**attempt
            if delay > 30:
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            delay = 2**attempt
        time.sleep(delay)


def activity_by_market(wallet):
    params = {"user": wallet, "limit": 1000}
    markets = defaultdict(list)
    cursors = set()
    for _ in range(100):
        page = get_json("https://data-api.polymarket.com/v2/activity", params)
        for event in page["data"]:
            if event["proxy_wallet"].lower() != wallet.lower():
                raise ValueError("Activity owner mismatch")
            if event.get("condition_id"):
                markets[event["condition_id"]].append(event)
        pagination = page["pagination"]
        cursor = pagination["next_cursor"]
        if pagination["has_more"] is False and cursor is None:
            return markets
        if (
            pagination["has_more"] is not True
            or not cursor
            or cursor in cursors
            or not page["data"]
        ):
            raise ValueError("Incomplete account activity")
        params["cursor"] = cursor
        cursors.add(cursor)
    raise ValueError("Account activity pagination exceeded")


def token_prices(token, start, end, directory, *, cache_result=True):
    # Never send a wallet or order identifier to the price-history service.
    digest = hashlib.sha256(f"{token}|{start}|{end}|1".encode()).hexdigest()
    path = directory / (digest + ".json.gz")
    if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    accumulated = {}
    clock = start - GAP
    while clock < end:
        stop = min(clock + 86400, end)
        result = get_json(
            "https://clob.polymarket.com/prices-history",
            {"market": token, "startTs": clock, "endTs": stop, "fidelity": 1},
        )
        for p in result["history"]:
            stamp, value = p["t"], float(p["p"])
            if not 0 <= value <= 1 or type(stamp) is not int:
                raise ValueError("Invalid historical price")
            if clock <= stamp <= stop:
                accumulated[stamp] = value
        clock = stop
    result = sorted(accumulated.items())
    if not cache_result:
        return result
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temp:
        temp.write(gzip.compress(json.dumps(result, separators=(",", ":")).encode()))
    os.replace(temp.name, path)
    return result


def money_units(value):
    return round(value * UNIT)


class Inventory(NamedTuple):
    at: int
    base: Decimal  # cash flows plus the $1 value of each filled complementary pair
    exposed: str
    excess: Decimal
    fills: dict
    entries: dict  # contemporaneous average entry of each non-dust holding
    sells: dict


def evidence(legs, events, settlement=None, final_units=None, marked_at=None):
    """Validate the whole market before using its intervening cash and share balances."""
    tokens = [r["token_id"] for r in legs]
    if len(tokens) == 2 and any(
        r["opposite_token_id"] not in tokens or r["opposite_token_id"] == r["token_id"]
        for r in legs
    ):
        return
    if len(tokens) not in (1, 2) or not events:
        return
    cash, stake = Decimal(0), Decimal(0)
    balances = dict.fromkeys(tokens, Decimal(0))
    purchased = balances.copy()
    averages = dict.fromkeys(tokens)
    intervals = []
    for t, batch in groupby(
        sorted(events, key=lambda e: e["timestamp"]), key=lambda e: e["timestamp"]
    ):
        trade_prices = {}
        sales = {}
        added, removed = set(), set()
        for event in batch:
            kind, token = event["type"], event.get("token_id")
            size, amount = Decimal(event["size"]), Decimal(event["usdc_size"])
            if not size.is_finite() or not amount.is_finite() or min(size, amount) < 0:
                return
            if (
                kind == "TRADE"
                and token in balances
                and event["side"] in {"BUY", "SELL"}
            ):
                buy = event["side"] == "BUY"
                price = Decimal(str(event["price"]))
                if not 0 <= price <= 1 or size <= 0:
                    return
                if buy:
                    prior = balances[token]
                    if prior <= 0:
                        averages[token] = price
                    elif averages[token] is not None:
                        averages[token] = (averages[token] * prior + price * size) / (
                            prior + size
                        )
                    added.add(token)
                else:
                    sales[token] = min(price, sales.get(token, price))
                    removed.add(token)
                balances[token] += size if buy else -size
                cash += -amount if buy else amount
                if buy:
                    purchased[token] += size
                    stake += amount
                trade_prices[token] = price
            elif kind == "REDEEM" and token in balances:
                balances[token] -= size
                removed.add(token)
                cash += amount
            elif kind in {"MERGE", "SPLIT"} and len(tokens) == 2:
                if size != amount:
                    return
                split = kind == "SPLIT"
                for key in tokens:
                    balances[key] += size if split else -size
                    if split:
                        purchased[key] += size
                        averages[key] = None  # a split prices the pair, not either side
                        added.add(key)
                    else:
                        removed.add(key)
                cash += -amount if split else amount
                if split:
                    stake += amount
            else:
                return
        if min(balances.values()) < -QUANTUM:
            return
        for token in added & removed:
            # Same-second mixed fills have no intra-second ordering in the activity API.
            averages[token] = None
        # Matching binary shares are worth their $1 complete-set redemption.
        paired = (
            max(Decimal(0), min(balances.values())) if len(tokens) == 2 else Decimal(0)
        )
        exposed = max(balances, key=balances.get)
        residual = max(Decimal(0), balances[exposed] - paired)
        entries = {
            key: averages[key]
            for key in tokens
            if balances[key] >= DUST and averages[key]
        }
        intervals.append(
            Inventory(t, cash + paired, exposed, residual, trade_prices, entries, sales)
        )
    close = max(r["last_event_at"] for r in legs)
    if settlement:
        close = max(close, settlement.closed)
    elif marked_at is not None:
        close = marked_at
    if (
        stake <= 0
        or intervals[0][0] != min(r["first_entry_at"] for r in legs)
        or intervals[-1][0] > close
    ):
        return
    if any(
        abs(purchased[r["token_id"]] - r["total_size"]) > QUANTUM
        or abs(balances[r["token_id"]] - r["current_size"]) > QUANTUM
        for r in legs
    ):
        return
    rounding = len(legs) * QUANTUM + Decimal("0.000001") * len(events)
    if settlement:
        redemption = sum(
            size * settlement.payouts[token] for token, size in balances.items()
        )
        if (
            abs(cash + redemption - Decimal(final_units) / UNIT)
            > rounding + len(legs) * QUANTUM
        ):
            return
    elif marked_at is not None:
        remaining_cost = sum(
            r["total_cost_usdc"]
            for r in legs
            if r["status"] != "CLOSED" and r["current_size"] > 0
        )
        if abs(cash + remaining_cost - sum(r["realized_pnl"] for r in legs)) > rounding:
            return
    else:
        tolerance = sum(r["current_size"] for r in legs) + rounding
        if abs(cash - sum(r["realized_pnl"] for r in legs)) > tolerance:
            return
    if intervals[-1][0] < close:
        # The last trade can precede a dust bookkeeping event or final settlement.
        if (
            not settlement
            and marked_at is None
            and any(abs(size) >= DUST for size in balances.values())
        ):
            return
        intervals.append(Inventory(close, cash, tokens[0], Decimal(0), {}, {}, {}))
    if settlement:
        i = bisect.bisect_left([point[0] for point in intervals], settlement.closed)
        if 0 < i < len(intervals) and intervals[i][0] > settlement.closed:
            intervals.insert(
                i, intervals[i - 1]._replace(at=settlement.closed, fills={}, sells={})
            )
    return intervals, float(stake)


def first_crash(intervals, tape, settlement):
    """Return the first underwater 99% crash and its P&L in units if observable."""

    for position, following in pairwise(intervals):
        if settlement and position.at >= settlement.closed:
            break
        if position.base >= 0 or not position.entries:
            continue
        latest = {}
        updates = defaultdict(dict)
        for token in position.entries.keys() | {position.exposed}:
            times, prices = tape(token)
            i = bisect.bisect_left(times, position.at)
            j = bisect.bisect_left(times, following.at)
            if i and position.at - times[i - 1] <= GAP:
                latest[token] = (times[i - 1], Decimal(str(prices[i - 1])))
            for idx in range(i, j):
                updates[times[idx]][token] = Decimal(str(prices[idx]))
        updates[position.at].update(position.fills)
        # A liquidating sale or final payout also prices the pre-exit inventory.
        boundary = following.sells
        if settlement and following.at == settlement.closed:
            boundary = settlement.payouts
        if boundary:
            updates[following.at].update(boundary)
        for at in sorted(updates):
            for token, price in updates[at].items():
                latest[token] = (at, price)
            live = {
                token: px for token, (stamp, px) in latest.items() if at - stamp <= GAP
            }
            if not any(
                token in live and live[token] * 100 <= entry
                for token, entry in position.entries.items()
            ):
                continue
            # Without a fresh opposite quote, require a loss even at its maximum payout.
            price = live.get(position.exposed)
            if price is None and position.excess:
                if money_units(position.base + position.excess) < 0:
                    return at, None
                continue
            pnl = money_units(position.base + position.excess * (price or 0))
            if pnl < 0:
                return at, pnl
    return None


def compress(points, start, end):
    """Keep each time bucket's endpoints and extremes, then drop flat interiors."""
    kept = []
    for _, group in groupby(
        points, key=lambda p: (p[0] - start) * 24 // max(1, end - start)
    ):
        bucket = list(group)
        indices = {
            0,
            len(bucket) - 1,
            min(range(len(bucket)), key=lambda i: bucket[i][1]),
            max(range(len(bucket)), key=lambda i: bucket[i][1]),
        }
        for i in sorted(indices):
            t, v = bucket[i]
            item = [t - start, money_units(v)]
            if len(kept) >= 2 and kept[-1][1] == kept[-2][1] == item[1]:
                kept[-1] = item
            else:
                kept.append(item)
    return kept


def reconstruct(
    legs, events, prices, *, settlement=None, final_units=None, marked_at=None
):
    start, end = (
        min(r["first_entry_at"] for r in legs),
        max(r["last_event_at"] for r in legs),
    )
    if settlement:
        end = max(end, settlement.closed)
    elif marked_at is not None:
        end = marked_at
    loss = (
        final_units < 0
        if final_units is not None
        else sum(r["realized_pnl"] for r in legs) < 0
    )
    out = {
        "opened": start,
        "mood": "losing" if loss else "unrated",
        "coverage": "close",
        "segments": [],
    }
    verified = evidence(legs, events, settlement, final_units, marked_at)
    if not verified:
        return out
    intervals, stake = verified
    out["stake"] = money_units(stake)
    tapes = {}

    def tape(token):
        if token not in tapes:
            points = prices(token, start, end)
            tapes[token] = ([p[0] for p in points], [p[1] for p in points])
        return tapes[token]

    if loss:
        crossed = first_crash(intervals, tape, settlement)
        if crossed is not None:
            out["crashAt"], out["crashPnl"] = crossed
    segments = []
    segment = []
    complete = True
    market_marks = 0

    def gap():
        nonlocal complete, segment
        complete = False
        if segment:
            segments.append(segment)
            segment = []

    def mark(t, value):
        if segment and segment[-1][0] == t:
            segment[-1] = (t, value)
        else:
            segment.append((t, value))

    for position, following in pairwise(intervals):
        t, token = position.at, position.exposed
        base, quantity = float(position.base), float(position.excess)
        fill_price = position.fills.get(token)
        stop = following.at
        if settlement and t >= settlement.closed:
            value = base + quantity * float(settlement.payouts[token])
            mark(t, value)
            mark(stop - 1, value)
            continue
        if quantity < float(DUST):
            mark(t, base)
            mark(stop - 1, base)
            continue
        times, values = tape(token)
        i, j = bisect.bisect_left(times, t), bisect.bisect_left(times, stop)
        observed = None
        if fill_price is not None:
            observed = t
            mark(t, base + quantity * float(fill_price))
        elif i and times[i - 1] >= t - GAP:
            observed = times[i - 1]
            mark(t, base + quantity * values[i - 1])
        for index in range(i, j):
            stamp = times[index]
            if stamp - (observed if observed is not None else t) > GAP:
                gap()
            mark(stamp, base + quantity * values[index])
            market_marks += 1
            observed = stamp
        if observed is None or stop - observed > GAP:
            gap()
        elif segment:
            mark(stop - 1, segment[-1][1])
    if segment:
        segments.append(segment)
    if not segments:
        return out
    peak, drop = 0.0, 0.0
    for seg in segments:
        for _, v in seg:
            peak = max(peak, v)
            drop = max(drop, peak - v)
    out["segments"] = [compress(seg, start, end) for seg in segments]
    complete = complete and market_marks >= 2
    out["coverage"] = "full" if complete else "partial"
    if complete:
        pct = 100 * drop / stake
        out["pullback"] = round(pct, 2)
        if not loss:
            out["mood"] = (
                "smooth" if pct < 5 else "troubling" if pct < 15 else "nerve-wracking"
            )
    return out


def retain_crash_times(jobs, path):
    """A live observation stays usable after a refresh, redemption, or price-tape gap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=15)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS crossings (
            market TEXT NOT NULL, opened INTEGER NOT NULL, observed INTEGER NOT NULL,
            PRIMARY KEY (market, opened))""")
        db.execute("""CREATE TABLE IF NOT EXISTS crossing_marks (
            market TEXT NOT NULL, opened INTEGER NOT NULL, observed INTEGER NOT NULL,
            pnl INTEGER NOT NULL, PRIMARY KEY (market, opened, observed))""")
        for legs, deal, _ in jobs:
            if deal["pnl"] >= 0:
                continue
            history = deal["history"]
            key = (legs[0]["condition_id"], history["opened"])
            observed = history.pop("crashAt", deal.get("markedAt"))
            pnl = history.pop("crashPnl", None)
            if (
                pnl is None
                and observed is not None
                and observed == deal.get("markedAt")
            ):
                pnl = deal["pnl"]
            end = deal.get("markedAt", deal.get("closed"))
            if observed is not None and history["opened"] <= observed <= end:
                db.execute(
                    """INSERT INTO crossings VALUES (?,?,?) ON CONFLICT(market,opened)
                    DO UPDATE SET observed=MIN(observed, excluded.observed)""",
                    (*key, observed),
                )
            saved = db.execute(
                "SELECT observed FROM crossings WHERE market=? AND opened=?", key
            ).fetchone()
            if saved and history["opened"] <= saved[0] <= end:
                deal["crashAt"] = saved[0]
                crash_key = (*key, saved[0])
                if observed == saved[0] and pnl is not None:
                    db.execute(
                        "INSERT OR IGNORE INTO crossing_marks VALUES (?,?,?,?)",
                        (*crash_key, pnl),
                    )
                mark = db.execute(
                    "SELECT pnl FROM crossing_marks WHERE market=? AND opened=? AND observed=?",
                    crash_key,
                ).fetchone()
                if mark:
                    deal["crashPnl"] = mark[0]


def populate(jobs, wallet, cache):
    activity = activity_by_market(wallet)

    def process(job):
        legs, deal, settlement = job
        marked_at = deal.get("markedAt")
        deal["history"] = reconstruct(
            legs,
            activity[legs[0]["condition_id"]],
            lambda token, start, end: token_prices(
                token, start, end, cache, cache_result=marked_at is None
            ),
            settlement=settlement,
            final_units=deal["pnl"],
            marked_at=marked_at,
        )

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(process, jobs))
    retain_crash_times(jobs, cache.parent / "observed-price-loss-v1.sqlite3")
