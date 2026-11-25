"""Confirm a final payout by matching Polymarket's market and order-book records."""

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.error import URLError

if __package__:
    from .deal_history import get_json
else:
    from deal_history import get_json


@dataclass(frozen=True)
class Settlement:
    closed: int
    payouts: dict


def confirm(condition, book, markets):
    if book["condition_id"] != condition or book["closed"] is not True:
        return None
    if len(markets) != 1:
        return None
    market = markets[0]
    if (
        market["conditionId"] != condition
        or market["closed"] is not True
        or not market.get("closedTime")
    ):
        return None
    status = market.get("umaResolutionStatus")
    if status and status != "resolved":
        return None
    ids = json.loads(market["clobTokenIds"])
    prices = list(map(Decimal, json.loads(market["outcomePrices"])))
    if len(ids) != 2 or len(set(ids)) != 2 or len(prices) != 2:
        return None
    payouts = dict(zip(ids, prices, strict=True))
    tokens = book["tokens"]
    if len(tokens) != 2 or {t["token_id"] for t in tokens} != set(ids):
        return None
    if any(Decimal(str(t["price"])) != payouts[t["token_id"]] for t in tokens):
        return None
    if sorted(prices) == [0, 1]:
        if any(t["winner"] is not (payouts[t["token_id"]] == 1) for t in tokens):
            return None
    elif prices != [Decimal("0.5")] * 2 or status != "resolved":
        return None
    stamp = datetime.fromisoformat(market["closedTime"])
    if stamp.utcoffset() is None:
        return None
    return Settlement(int(stamp.timestamp()), payouts)


def lookup(rows, dust, *, activity=None, cache_path=None):
    required = {
        r["condition_id"]
        for r in rows
        if r["current_size"] >= dust
        or (r["current_size"] > 0 and r["status"] != "CLOSED")
    }
    # A material redemption or merge can post after the payout was determined,
    # even when every share has since disappeared from the position feed.
    conditions = {r["condition_id"] for r in rows}
    candidates = required | {
        condition
        for condition, events in (activity or {}).items()
        if condition in conditions
        and any(e["type"] in {"REDEEM", "MERGE"} and e["size"] >= dust for e in events)
    }
    if any(not re.fullmatch(r"0x[0-9a-fA-F]{64}", c) for c in candidates):
        raise ValueError("Invalid condition identifier")

    def one(condition):
        try:
            book = get_json("https://clob.polymarket.com/markets/" + condition, {})
            if book["closed"] is not True:
                return None
            markets = get_json(
                "https://gamma-api.polymarket.com/markets",
                {"condition_ids": condition, "closed": "true"},
            )
        except (URLError, TimeoutError, ConnectionError):
            if condition in required:
                raise
            # Optional historical timing can fall back to verified wallet activity.
            return None
        return confirm(condition, book, markets)

    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
    # Only payouts confirmed by both sources enter this cache. Open and
    # unconfirmed markets are checked again at every refresh.
    with closing(sqlite3.connect(cache_path or ":memory:", timeout=15)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS settlements (
            condition TEXT PRIMARY KEY, closed INTEGER NOT NULL, payouts TEXT NOT NULL)""")
        saved = db.execute("SELECT condition, closed, payouts FROM settlements")
        result = {
            condition: Settlement(
                closed, {t: Decimal(v) for t, v in json.loads(payouts).items()}
            )
            for condition, closed, payouts in saved
            if condition in candidates
        }
        pending = sorted(candidates - result.keys())
        with ThreadPoolExecutor(max_workers=4) as workers:
            fresh = {
                c: s for c, s in zip(pending, workers.map(one, pending)) if s is not None
            }
        db.executemany(
            "INSERT OR IGNORE INTO settlements VALUES (?,?,?)",
            [(c, s.closed, json.dumps(s.payouts, default=str)) for c, s in fresh.items()],
        )
    return {**result, **fresh}
