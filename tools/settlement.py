"""Confirm a final payout by matching Polymarket's market and order-book records."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

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


def lookup(rows, dust):
    candidates = sorted(
        {
            r["condition_id"]
            for r in rows
            if r["current_size"] >= dust
            or (r["current_size"] > 0 and r["status"] != "CLOSED")
        }
    )

    def one(condition):
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition):
            raise ValueError("Invalid condition identifier")
        book = get_json("https://clob.polymarket.com/markets/" + condition, {})
        if book["closed"] is not True:
            return None
        markets = get_json(
            "https://gamma-api.polymarket.com/markets",
            {"condition_ids": condition, "closed": "true"},
        )
        return confirm(condition, book, markets)

    with ThreadPoolExecutor(max_workers=4) as workers:
        return {
            c: s
            for c, s in zip(candidates, workers.map(one, candidates))
            if s is not None
        }
