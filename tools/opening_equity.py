"""Attach the last verified account valuation before each deal's first entry."""

import bisect
import json
import math
import sqlite3
from contextlib import closing
from decimal import Decimal


def first_funding_checkpoint(path, wallet):
    """Load the host's independently attested collateral and outcome-balance check."""
    if path is None or not path.is_file():
        return None
    try:
        point = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(point, dict)
        or point.get("source") != "polygon-archive-before-first-fill"
        or not isinstance(point.get("wallet"), str)
        or point["wallet"].lower() != wallet.lower()
        or any(
            type(point.get(k)) is not int
            for k in ("at", "firstEntry", "equityAtoms", "block")
        )
        or not 0 < point["at"] < point["firstEntry"]
        or point["equityAtoms"] < 0
        or point["block"] <= 0
    ):
        return None
    return point


def attach_opening_equity(ledger, wallet, deals, first_funding_path=None):
    # The trading ledger measures marked account equity in millionths of a dollar.
    # The public snapshot uses ten-thousandths, the same units as deal P&L.
    query = """
        SELECT prepared_at, json_extract(snapshot, '$.equity'),
               json_extract(snapshot, '$.evidence')
        FROM rn2_actions
        WHERE json_valid(snapshot) AND json_valid(action)
          AND lower(json_extract(snapshot, '$.wallet')) = lower(?)
          AND json_type(snapshot, '$.complete') = 'true'
          AND json_type(snapshot, '$.equity') = 'integer'
          AND json_type(snapshot, '$.evidence') = 'array'
          AND json_extract(snapshot, '$.snapshot_id') = json_extract(action, '$.snapshot_id')
    """
    observations = {}
    with closing(
        sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        for prepared, equity, evidence in db.execute(query, (wallet,)):
            clocks = json.loads(evidence)
            if (
                equity < 0
                or not clocks
                or not all(
                    isinstance(c, dict)
                    and type(c.get("requested_at")) in (int, float)
                    and type(c.get("received_at")) in (int, float)
                    and 0 < c["requested_at"] <= c["received_at"] <= prepared
                    and math.isfinite(c["received_at"])
                    for c in clocks
                )
            ):
                continue
            received = max(c["received_at"] for c in clocks)
            # Two contradictory readings with the exact same observation time
            # cannot establish a single opening balance.
            if received in observations and observations[received] != equity:
                observations[received] = None
            else:
                observations[received] = equity

    times = sorted(observations)
    initial = first_funding_checkpoint(first_funding_path, wallet)
    for deal in deals:
        history = deal["history"]
        opened = history["opened"]
        index = bisect.bisect_right(times, opened) - 1
        if (
            initial
            and opened == initial["firstEntry"]
            and (index < 0 or initial["at"] > times[index])
        ):
            history["openingEquity"] = {
                "at": initial["at"],
                "value": round(Decimal(initial["equityAtoms"]) / 100),
            }
            continue
        if index >= 0:
            at = times[index]
            atoms = observations[at]
            if atoms is not None:
                history["openingEquity"] = {
                    "at": math.ceil(at),
                    "value": round(Decimal(atoms) / 100),
                }
