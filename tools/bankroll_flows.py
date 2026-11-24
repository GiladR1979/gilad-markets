"""Read external cash flows for bankroll returns without publishing wallet identifiers."""

import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from decimal import Decimal
from http.client import HTTPException
from pathlib import Path

if __package__:
    from .deal_history import UNIT, get_json
else:
    from deal_history import UNIT, get_json

MAX_PUBLIC_UNITS = 2**53 - 1


class FundingHistoryUnavailable(ValueError):
    """The external cash-flow history could not be verified."""


def fetch_external_flows(wallet, checked):
    """Return net cash in 1/10,000 USD, dated to the second and bounded by checked."""
    if not re.fullmatch(r"0x[0-9a-f]{40}", wallet, re.IGNORECASE):
        raise ValueError("Invalid wallet for funding history")
    if type(checked) is not int or checked < 1:
        raise ValueError("Invalid snapshot time for funding history")

    params = {
        "user": wallet,
        "type": "DEPOSIT,WITHDRAWAL",
        "exclude_deposits_withdrawals": "false",
        "start": 1,
        "end": checked,
        "limit": 1000,
    }
    totals = defaultdict(Decimal)
    transactions, events, cursors = {}, {}, set()
    while True:
        try:
            page = get_json("https://data-api.polymarket.com/v2/activity", params)
        except (OSError, HTTPException, ValueError) as exc:
            raise FundingHistoryUnavailable("Could not read funding history") from exc
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise FundingHistoryUnavailable("Invalid funding activity page")
        for row in page["data"]:
            if not isinstance(row, dict) or not isinstance(
                row.get("proxy_wallet"), str
            ):
                raise FundingHistoryUnavailable("Missing funding activity owner")
            if row["proxy_wallet"].lower() != wallet.lower():
                raise FundingHistoryUnavailable("Funding activity owner mismatch")
            kind, at = row.get("type"), row.get("timestamp")
            if kind not in ("DEPOSIT", "WITHDRAWAL") or type(at) is not int or at < 1:
                raise FundingHistoryUnavailable(
                    "Invalid funding activity type or timestamp"
                )
            if at > checked:
                continue
            tx = row.get("transaction_hash")
            if not isinstance(tx, str) or not re.fullmatch(
                r"0x[0-9a-f]{64}", tx, re.IGNORECASE
            ):
                raise FundingHistoryUnavailable("Invalid funding transaction identity")
            tx = tx.lower()
            raw = row.get("usdc_size")
            if (
                type(raw) not in (Decimal, int)
                or not Decimal(raw).is_finite()
                or not 0 < raw <= Decimal(MAX_PUBLIC_UNITS) / UNIT
            ):
                raise FundingHistoryUnavailable("Invalid funding amount")
            signed = Decimal(raw) if kind == "DEPOSIT" else -Decimal(raw)

            # v2 normally supplies a transaction and direction. If explicit log
            # indices are present, multiple same-direction transfers can share a tx.
            index = row.get("log_index")
            if index is not None and (type(index) is not int or index < 0):
                raise FundingHistoryUnavailable("Invalid funding log index")
            version = (at, index is not None)
            if transactions.setdefault(tx, version) != version:
                raise FundingHistoryUnavailable(
                    "Conflicting funding transaction metadata"
                )
            key = (tx, kind if index is None else index)
            if key in events:
                if events[key] != signed:
                    raise FundingHistoryUnavailable("Conflicting funding activity")
                continue
            events[key] = signed
            totals[at] += signed

        pagination = page.get("pagination")
        if not isinstance(pagination, dict) or "next_cursor" not in pagination:
            raise FundingHistoryUnavailable("Incomplete funding pagination")
        more, cursor = pagination.get("has_more"), pagination["next_cursor"]
        if more is False and cursor is None:
            break
        if (
            more is not True
            or not isinstance(cursor, str)
            or not cursor
            or cursor in cursors
            or not page["data"]
        ):
            raise FundingHistoryUnavailable("Incomplete funding pagination")
        cursors.add(cursor)
        params["cursor"] = cursor

    flows = []
    for at, total in sorted(totals.items()):
        amount = round(total * UNIT)
        if abs(amount) > MAX_PUBLIC_UNITS:
            raise FundingHistoryUnavailable(
                "Funding amount exceeds dashboard precision"
            )
        if amount:
            flows.append({"at": at, "amount": amount})
    return flows


def _cached_period(path, wallet, last_close, checked, live):
    """Atomically replace a complete wallet history and read the best covered one."""
    if live is None:
        if not path.is_file():
            return None
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=5)
    with closing(db), db:
        if live is not None:
            db.execute("""CREATE TABLE IF NOT EXISTS funding_history (
                wallet TEXT PRIMARY KEY, checked INTEGER NOT NULL, flows TEXT NOT NULL)""")
            db.execute(
                """INSERT INTO funding_history (wallet,checked,flows) VALUES (?,?,?)
                ON CONFLICT(wallet) DO UPDATE SET checked=excluded.checked,flows=excluded.flows
                WHERE excluded.checked >= funding_history.checked""",
                (wallet, checked, json.dumps(live, separators=(",", ":"))),
            )
        row = db.execute(
            "SELECT checked,flows FROM funding_history WHERE wallet=? AND checked>=?",
            (wallet, last_close),
        ).fetchone()
    if row is None:
        return None
    through, payload = row
    records = json.loads(payload)
    if (
        type(through) is not int
        or through < last_close
        or not isinstance(records, list)
    ):
        raise FundingHistoryUnavailable("Invalid cached funding coverage")
    result, previous = [], 0
    for entry in records:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"at", "amount"}
            or type(entry["at"]) is not int
            or not previous < entry["at"] <= through
            or type(entry["amount"]) is not int
            or not 0 < abs(entry["amount"]) <= MAX_PUBLIC_UNITS
        ):
            raise FundingHistoryUnavailable("Invalid cached funding history")
        previous = entry["at"]
        if previous <= last_close:
            result.append(entry)
    return result


def funding_for_period(wallet, checked, last_close, cache_file):
    """Return verified cash flows through last_close, including during an API outage."""
    if type(last_close) is not int or last_close < 0:
        raise ValueError("Invalid closing cutoff for funding history")
    if last_close > checked:
        raise FundingHistoryUnavailable("The funding period ends after this snapshot")
    if last_close == 0:
        return []
    failure = None
    try:
        live = fetch_external_flows(wallet, checked)
    except FundingHistoryUnavailable as exc:
        failure, live = exc, None

    if cache_file is not None:
        try:
            cached = _cached_period(
                Path(cache_file), wallet.lower(), last_close, checked, live
            )
        except (OSError, sqlite3.Error, ValueError, TypeError):
            # A cache failure leaves a complete, freshly fetched history usable.
            cached = None
        if cached is not None:
            return cached
    if live is None:
        raise FundingHistoryUnavailable(
            "No verified funding history covers this close"
        ) from failure
    return [entry for entry in live if entry["at"] <= last_close]
