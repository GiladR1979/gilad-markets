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


def build(rows, method, start, sizes, latest, now):
    markets, tokens = collections.defaultdict(list), {}
    for row in rows:
        token = row["token_id"]
        if token in tokens:
            if tokens[token] == row:
                continue
            raise ValueError(
                "Position moved between lifecycle views; retry next refresh"
            )
        tokens[token] = row
        if number(row["current_size"]) < 0 or number(row["total_size"]) <= 0:
            raise ValueError("Unexpected position quantity")
        if row["status"] not in {"CLOSED", "OPEN", "REDEEMABLE"}:
            raise ValueError("Unknown position lifecycle")
        markets[row["condition_id"]].append(row)
    deals = []
    for legs in markets.values():
        if any(number(r["current_size"]) >= DUST for r in legs):
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
        deals.append(
            dict(
                market=title,
                closed=last,
                pnl=int(pnl),
                legs=len(legs),
                method=is_latest,
                verified=is_bot,
                dust=any(number(r["current_size"]) > 0 for r in legs),
            )
        )
    deals.sort(key=lambda r: (-r["closed"], r["market"], r["pnl"]))
    return dict(
        version=1,
        checked=int(now),
        method=method,
        methodSince=int(start) if start is not None else None,
        currency="USD",
        unit=PNL_SCALE,
        dustShares=str(DUST),
        deals=deals,
    )


def snapshot(ledger, repository, gh):
    tag, commit = release_tag(repository, gh)
    wallet, method, start, sizes, latest = release_and_entries(ledger, commit)
    rows = fetch_positions(wallet, "CLOSED") + fetch_positions(wallet, "OPEN")
    if not rows:
        raise ValueError("Trading account history unavailable")
    return {
        **build(rows, method, start, sizes, latest, int(time.time())),
        "tag": tag,
        "commit": commit,
    }


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
