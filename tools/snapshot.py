"""Publishable closed-market performance from public positions and read-only entry evidence."""

import argparse
import collections
import json
import re
import sqlite3
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

if __package__:
    from .settlement import lookup
else:
    from settlement import lookup


API = "https://data-api.polymarket.com/v2/positions"
DUST = Decimal("0.01")
SHARE_SCALE = 1_000_000
PNL_SCALE = 10_000


def number(value):
    n = Decimal(str(value))
    if not n.is_finite():
        raise ValueError("Non-finite amount")
    return n


def release_tag(repository, gh):
    result = subprocess.run(
        [gh, "api", "--paginate", "--slurp", f"repos/{repository}/tags?per_page=100"],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=45,
    )
    tags = [
        (int(t["name"][10:]), t)
        for page in json.loads(result.stdout)
        for t in page
        if re.fullmatch(r"Successful[1-9][0-9]*", t["name"])
    ]
    if not tags:
        raise ValueError("No numbered release tag found")
    tag = max(tags, key=lambda item: item[0])[1]
    commit = tag["commit"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid tagged commit")
    return tag["name"], commit


def release_and_entries(path, tagged_commit):
    with sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3
    ) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        release_body = db.execute(
            "SELECT body FROM rn2_meta WHERE key='api_first_rewrite_activation'"
        ).fetchone()[0]
        release = json.loads(release_body)
        if not db.execute(
            "SELECT 1 FROM rn2_release_history WHERE activation_id=? AND body=?",
            (release["activation_id"], release_body),
        ).fetchone():
            raise ValueError("Current release is missing from release history")
        history = [
            json.loads(row[0])
            for row in db.execute("SELECT body FROM rn2_release_history")
        ]

        def activated(item):
            return item.get("maintenance", {}).get(
                "created_at", item["manifest"]["created_at"]
            )

        history.sort(key=activated)
        if history[-1]["activation_id"] != release["activation_id"]:
            raise ValueError("Deployment history and current activation disagree")
        windows, method = [], "Tagged release"
        for index, item in enumerate(history):
            if item["manifest"]["source_commit"] != tagged_commit:
                continue
            begin = activated(item)
            end = (
                activated(history[index + 1])
                if index + 1 < len(history)
                else float("inf")
            )
            windows.append((begin, end))
            label = re.fullmatch(
                r"rn2-([a-z0-9-]+)-\d{8}-[a-f0-9]+", item["manifest"]["artifact_id"]
            )
            if label:
                method = label[1].replace("-", " ").capitalize()
        rows = db.execute("""
            SELECT e.body, o.condition, a.action,
                   a.action_id, a.prepared_at, p.body
            FROM rn2_executions e
            JOIN rn2_orders o ON o.order_id=e.order_id
            JOIN rn2_actions a ON a.action_id=o.action_id
            LEFT JOIN rn2_pairs p ON p.pair_id=CASE json_extract(a.action,'$.kind')
                WHEN 'enter_pair' THEN a.action_id ELSE json_extract(a.action,'$.pair_id') END
            WHERE json_extract(e.body,'$.side')='BUY'
        """)
        sizes = collections.defaultdict(int)
        latest = collections.defaultdict(int)
        for body, condition, action_body, action_id, prepared, pair_body in rows:
            execution, action = json.loads(body), json.loads(action_body)
            if condition != execution["condition"] or condition != action["condition"]:
                raise ValueError("Execution condition mismatch")
            key = condition, execution["token"]
            qty = execution["quantity"]
            if type(qty) is not int or qty <= 0:
                raise ValueError("Invalid confirmed buy quantity")
            sizes[key] += qty
            if pair_body:
                pair = json.loads(pair_body)
                parent = (
                    action_id if action["kind"] == "enter_pair" else action["pair_id"]
                )
                if (
                    pair["pair_id"] != parent
                    or pair["condition"] != condition
                    or execution["token"] not in pair["tokens"]
                ):
                    raise ValueError("Pair attribution mismatch")
                if prepared >= pair["created_at"] and any(
                    begin <= pair["created_at"] < end for begin, end in windows
                ):
                    latest[key] += qty
        start = windows[0][0] if windows else None
        return release["wallet"], method, start, sizes, latest


def fetch_positions(wallet, state):
    params = dict(
        user=wallet,
        status=state,
        limit=1000,
        sort_by="TIMESTAMP",
        filter_type="TOKENS",
        filter_amount="0",
    )
    if state == "OPEN":
        params["include_archived"] = "true"
    seen_cursors, tokens, rows = set(), set(), []
    for _ in range(80):
        request = Request(
            API + "?" + urlencode(params),
            headers={"User-Agent": "GiladPublicDashboard/1.0"},
        )
        with urlopen(request, timeout=15) as response:
            page = json.load(response, parse_float=Decimal)
        batch, pagination = page["data"], page["pagination"]
        if not isinstance(batch, list) or type(pagination["has_more"]) is not bool:
            raise ValueError("Incomplete positions page")
        for row in batch:
            token = row["token_id"]
            if row["proxy_wallet"].lower() != wallet.lower() or token in tokens:
                raise ValueError("Position identity mismatch")
            tokens.add(token)
            rows.append(row)
        cursor = pagination.get("next_cursor")
        if not pagination["has_more"] and cursor is None:
            return rows
        if (
            not pagination["has_more"]
            or not cursor
            or cursor in seen_cursors
            or not batch
        ):
            raise ValueError("Incomplete positions pagination")
        seen_cursors.add(cursor)
        params["cursor"] = cursor
    raise ValueError("Position history exceeds pagination budget")


def matched(row, sizes):
    quantity = sizes.get((row["condition_id"], row["token_id"]), 0)
    # The public API displays lifetime shares to four places. Strictly less than one quantum.
    return (
        quantity > 0 and abs(number(row["total_size"]) * SHARE_SCALE - quantity) < 100
    )


def price_rule_loss(legs):
    """Mark a 99% price decline only when the entire filled market position is losing."""
    held = [
        r for r in legs if r["status"] != "CLOSED" and number(r["current_size"]) > 0
    ]
    triggers = []
    for r in held:
        if (
            number(r["current_size"]) < DUST
            or r.get("avg_price") is None
            or r.get("current_price") is None
        ):
            continue
        entry, mark = number(r["avg_price"]), number(r["current_price"])
        if 0 < entry <= 1 and 0 <= mark <= entry / 100:
            triggers.append((mark / entry, str(r["token_id"]), r))
    if not triggers:
        return None
    paired = Decimal(0)
    if len(held) > 1:
        if len(held) != 2 or any(
            r.get("opposite_token_id") != held[1 - i]["token_id"]
            for i, r in enumerate(held)
        ):
            return None
        paired = min(number(r["current_size"]) for r in held)
    net = sum(number(r["realized_pnl"]) for r in legs) + paired
    for r in held:
        net -= number(r["total_cost_usdc"])
        excess = number(r["current_size"]) - paired
        if excess:
            if (
                r.get("current_price") is None
                or not 0 <= number(r["current_price"]) <= 1
            ):
                return None
            net += excess * number(r["current_price"])
    units = round(net * PNL_SCALE)
    if units >= 0:
        return None
    _, _, trigger = min(triggers)
    return units, {
        "outcome": trigger["outcome"],
        "entry": float(trigger["avg_price"]),
        "mark": float(trigger["current_price"]),
    }


def reconcile_positions(rows):
    """Count one holding when both provider views report the same accounting."""
    accounting = (
        "proxy_wallet",
        "condition_id",
        "outcome_index",
        "opposite_token_id",
        "first_entry_at",
        "last_event_at",
        "total_size",
        "current_size",
        "avg_price",
        "entry_cost_usdc",
        "entry_fees_usdc",
        "total_cost_usdc",
        "realized_pnl",
    )
    tokens = {}
    for row in rows:
        token = row["token_id"]
        previous = tokens.get(token)
        if previous is None or previous == row:
            tokens[token] = row
            continue
        active, closed = (
            (previous, row) if row["status"] == "CLOSED" else (row, previous)
        )
        if (
            closed["status"] != "CLOSED"
            or active["status"] not in {"OPEN", "REDEEMABLE"}
            or any(active.get(k) != closed.get(k) for k in accounting)
        ):
            raise ValueError(
                "Position moved between lifecycle views; retry next refresh"
            )
        # The closed view reports realized returns even when shares remain. The
        # active view carries the mark and remaining basis for those same shares.
        tokens[token] = active if number(active["current_size"]) > 0 else closed
    return tokens.values()


def build(
    rows, method, start, sizes, latest, now, *, history_jobs=None, settlements=None
):
    markets = collections.defaultdict(list)
    for row in reconcile_positions(rows):
        if number(row["current_size"]) < 0 or number(row["total_size"]) <= 0:
            raise ValueError("Unexpected position quantity")
        if row["status"] not in {"CLOSED", "OPEN", "REDEEMABLE"}:
            raise ValueError("Unknown position lifecycle")
        markets[row["condition_id"]].append(row)
    deals, provisional = [], []
    for legs in markets.values():
        settlement = (settlements or {}).get(legs[0]["condition_id"])
        if settlement and (
            settlement.closed > now
            or any(r["token_id"] not in settlement.payouts for r in legs)
        ):
            raise ValueError("Settlement identity or chronology mismatch")
        marked = None
        if not settlement and any(number(r["current_size"]) >= DUST for r in legs):
            marked = price_rule_loss(legs)
            if marked is None:
                continue
        legs.sort(key=lambda row: (row["outcome_index"], row["token_id"]))
        last = max(int(r["last_event_at"]) for r in legs)
        if any(
            not 0 < int(r["first_entry_at"]) <= int(r["last_event_at"]) <= now + 60
            for r in legs
        ):
            raise ValueError("Position chronology mismatch")
        title = legs[0]["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > 1000:
            raise ValueError("Market title unavailable")
        pnl = sum(number(r["realized_pnl"]) for r in legs) * PNL_SCALE
        if pnl != pnl.to_integral_value():
            raise ValueError("Provider P&L precision changed")
        if settlement:
            # CLOSED legs already include their final result, even if a residual token still exists.
            additional = sum(
                number(r["current_size"]) * settlement.payouts[r["token_id"]]
                - number(r["total_cost_usdc"])
                for r in legs
                if r["status"] != "CLOSED" and number(r["current_size"]) > 0
            )
            pnl += round(additional * PNL_SCALE)
            last = max(last, settlement.closed)
        is_bot = all(matched(r, sizes) for r in legs)
        is_latest = (
            start is not None
            and is_bot
            and all(
                number(r["first_entry_at"]) >= start
                and matched(r, latest)
                and latest[(r["condition_id"], r["token_id"])]
                == sizes[(r["condition_id"], r["token_id"])]
                for r in legs
            )
        )
        # Only these public market/result fields cross the publication boundary.
        deal = dict(
            market=title,
            closed=last,
            pnl=int(pnl),
            legs=len(legs),
            method=is_latest,
            verified=is_bot,
            dust=all(number(r["current_size"]) < DUST for r in legs)
            and any(number(r["current_size"]) > 0 for r in legs),
        )
        if settlement:
            deal["settled"] = True
        if marked:
            if last > now:
                raise ValueError("Price observation predates the position")
            del deal["closed"]
            deal.update(markedAt=int(now), pnl=marked[0], priceRule=marked[1])
            provisional.append(deal)
        else:
            deals.append(deal)
        if history_jobs is not None:
            history_jobs.append((legs, deal, settlement))
    deals.sort(key=lambda r: (-r["closed"], r["market"], r["pnl"]))
    provisional.sort(key=lambda r: (r["market"], r["pnl"]))
    return dict(
        version=1,
        checked=int(now),
        method=method,
        methodSince=int(start) if start is not None else None,
        currency="USD",
        unit=PNL_SCALE,
        dustShares=str(DUST),
        deals=deals,
        provisional=provisional,
    )


def snapshot(ledger, repository, gh, *, price_cache=None):
    if __package__:
        from .deal_history import populate
    else:
        from deal_history import populate
    tag, commit = release_tag(repository, gh)
    wallet, method, start, sizes, latest = release_and_entries(ledger, commit)
    rows = fetch_positions(wallet, "CLOSED") + fetch_positions(wallet, "OPEN")
    if not rows:
        raise ValueError("Trading account history unavailable")
    history_jobs = []
    result = build(
        rows,
        method,
        start,
        sizes,
        latest,
        int(time.time()),
        history_jobs=history_jobs,
        settlements=lookup(rows, DUST),
    )
    cache = (
        price_cache or Path(__file__).resolve().parents[1] / ".local" / "price-cache-v1"
    )
    populate(history_jobs, wallet, cache)
    return {**result, "tag": tag, "commit": commit}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tag-repo", required=True)
    parser.add_argument("--gh", default="gh")
    args = parser.parse_args()
    result = snapshot(args.ledger, args.tag_repo, args.gh)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
