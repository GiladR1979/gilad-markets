"""Read external cash flows for bankroll returns without publishing wallet identifiers."""

import re
from collections import defaultdict
from decimal import Decimal
from http.client import HTTPException

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
