"""Publishable closed-market performance from public positions and read-only entry evidence."""

import argparse
import bisect
import collections
import json
import re
import sqlite3
import subprocess
import time
from decimal import Decimal
from pathlib import Path

if __package__:
    from .deal_history import get_json
    from .settlement import lookup
else:
    from deal_history import get_json
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


def release_tags(repository, gh):
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
    ordered = [
        (t["name"], t["commit"]["sha"])
        for _, t in sorted(tags, key=lambda item: item[0])
    ]
    if any(not re.fullmatch(r"[0-9a-f]{40}", sha) for _, sha in ordered):
        raise ValueError("Invalid tagged commit")
    return ordered


def release_tag(repository, gh):
    return release_tags(repository, gh)[-1]


def includes_commit(tagged, deployed, repository, gh, checkout):
    if not re.fullmatch(r"[0-9a-f]{7,40}", deployed):
        raise ValueError("Invalid deployment commit")
    if checkout:
        result = subprocess.run(
            ["git", "-C", checkout, "merge-base", "--is-ancestor", tagged, deployed],
            check=False,
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode in (0, 1):
            return result.returncode == 0
    result = subprocess.run(
        [
            gh,
            "api",
            "--cache",
            "24h",
            "--jq",
            ".status",
            f"repos/{repository}/compare/{tagged}...{deployed}?per_page=1",
        ],
        check=False,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode and b"HTTP 404" in result.stderr:
        # A host-only commit may be absent on GitHub. It cannot establish a
        # new boundary without evidence, but an existing period continues.
        return False
    result.check_returncode()
    status = result.stdout.decode().strip()
    if status not in {"identical", "ahead", "behind", "diverged"}:
        raise ValueError("Unknown commit comparison")
    return status in {"identical", "ahead"}


def activation(item):
    return item.get("maintenance", {}).get("created_at", item["manifest"]["created_at"])


def tag_periods(history, tags, includes=None):
    """Open a period at the first deployment of the tag or a following commit."""
    periods = {}
    index = 0
    for tag, sha in tags:  # numeric order; bundled tags can share a boundary
        for i in range(index, len(history)):
            manifest = history[i]["manifest"]
            deployed = manifest["source_commit"]
            if deployed == sha or (includes and includes(sha, deployed)):
                periods[tag] = (activation(history[i]), manifest["artifact_id"])
                index = i
                break
    return periods


def period_entries(path, tags, includes=None):
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

        history.sort(key=activation)
        if history[-1]["activation_id"] != release["activation_id"]:
            raise ValueError("Deployment history and current activation disagree")
        rows = db.execute("""
            SELECT e.body, o.condition, a.action,
                   a.action_id, a.prepared_at, p.body
            FROM rn2_executions e
            JOIN rn2_orders o ON o.order_id=e.order_id
            JOIN rn2_actions a ON a.action_id=o.action_id
            LEFT JOIN rn2_pairs p ON p.pair_id=CASE json_extract(a.action,'$.kind')
                WHEN 'enter_pair' THEN a.action_id ELSE json_extract(a.action,'$.pair_id') END
            WHERE json_extract(e.body,'$.side')='BUY'
        """).fetchall()
    periods = tag_periods(history, tags, includes)
    names = list(periods)
    boundaries = [periods[name][0] for name in names]
    sizes = collections.Counter()
    purchases = {name: collections.Counter() for name in names}
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
            parent = action_id if action["kind"] == "enter_pair" else action["pair_id"]
            if (
                pair["pair_id"] != parent
                or pair["condition"] != condition
                or execution["token"] not in pair["tokens"]
            ):
                raise ValueError("Pair attribution mismatch")
            index = bisect.bisect_right(boundaries, pair["created_at"]) - 1
            if prepared >= pair["created_at"] and index >= 0:
                purchases[names[index]][key] += qty
    return release["wallet"], sizes, periods, purchases


def method_label(artifact):
    label = re.fullmatch(r"rn2-([a-z0-9-]+)-\d{8}-[a-f0-9]+", artifact)
    return label[1].replace("-", " ").capitalize() if label else "Tagged release"


def release_and_entries(path, tagged_commit, *, include_tag_status=False):
    wallet, sizes, periods, purchases = period_entries(path, [("tag", tagged_commit)])
    start, artifact = periods.get("tag", (None, ""))
    entries = wallet, method_label(artifact), start, sizes, purchases.get("tag", {})
    return (*entries, start is not None) if include_tag_status else entries


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
        page = get_json(API, params, timeout=15)
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


def belongs_to_period(legs, sizes, begin, purchases):
    # Callers have already reconciled lifetime sizes to every provider leg.
    return begin is not None and all(
        number(r["first_entry_at"]) >= begin
        and purchases.get((r["condition_id"], r["token_id"]), 0)
        == sizes[(r["condition_id"], r["token_id"])]
        for r in legs
    )


def build(
    rows,
    method,
    start,
    sizes,
    latest,
    now,
    *,
    history_jobs=None,
    settlements=None,
    tag=None,
    prior_periods=None,
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
        material = any(number(r["current_size"]) >= DUST for r in legs)
        if not settlement and material:
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
            if material:
                last = max(last, settlement.closed)
        is_bot = all(matched(r, sizes) for r in legs)

        is_latest = is_bot and belongs_to_period(legs, sizes, start, latest)
        # Only these public market/result fields cross the publication boundary.
        deal = dict(
            market=title,
            closed=last,
            pnl=int(pnl),
            legs=len(legs),
            method=is_latest,
            verified=is_bot,
            dust=not material and any(number(r["current_size"]) > 0 for r in legs),
        )
        if is_latest and tag:
            deal["releaseTag"] = tag
        elif is_bot:
            for name, (begin, purchases) in (prior_periods or {}).items():
                if belongs_to_period(legs, sizes, begin, purchases):
                    deal["releaseTag"] = name
                    break
        if settlement and material:
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
        from .bankroll_flows import FundingHistoryUnavailable, funding_for_period
        from .deal_history import activity_by_market, populate
        from .opening_equity import attach_opening_equity
    else:
        from bankroll_flows import FundingHistoryUnavailable, funding_for_period
        from deal_history import activity_by_market, populate
        from opening_equity import attach_opening_equity
    tags = release_tags(repository, gh)
    tag, commit = tags[-1]
    # Rolling attribution is enabled starting with Successful8.
    rolling = [(name, sha) for name, sha in tags if int(name[10:]) >= 8]
    source_path = Path(__file__).resolve().parents[1] / ".local" / "release-source.txt"
    checkout = (
        source_path.read_text(encoding="utf-8").strip()
        if source_path.is_file()
        else None
    )
    wallet, sizes, periods, purchases = period_entries(
        ledger,
        rolling or [(tag, commit)],
        lambda base, head: includes_commit(base, head, repository, gh, checkout),
    )
    start, artifact = periods.get(tag, (None, ""))
    rows = fetch_positions(wallet, "CLOSED") + fetch_positions(wallet, "OPEN")
    if not rows:
        raise ValueError("Trading account history unavailable")
    cache = (
        price_cache or Path(__file__).resolve().parents[1] / ".local" / "price-cache-v1"
    )
    activity = activity_by_market(wallet)
    history_jobs = []
    result = build(
        rows,
        method_label(artifact),
        start,
        sizes,
        purchases.get(tag, {}),
        int(time.time()),
        history_jobs=history_jobs,
        tag=tag,
        prior_periods={
            name: (begin, purchases[name])
            for name, (begin, _) in periods.items()
            if name != tag
        },
        settlements=lookup(
            rows,
            DUST,
            activity=activity,
            cache_path=cache.parent / "confirmed-settlement-v1.sqlite3",
        ),
    )
    populate(history_jobs, wallet, cache, activity=activity)
    result["deals"].sort(key=lambda r: (-r["closed"], r["market"], r["pnl"]))
    attach_opening_equity(
        ledger,
        wallet,
        result["deals"],
        cache.parent / "verified-opening-equity-v1.json",
    )
    # Only publish transfers that could affect a selectable closed-deal period.
    beginnings = [
        row["history"]["openingEquity"]["at"]
        for row in result["deals"]
        if "openingEquity" in row["history"]
    ]
    if beginnings:
        first = min(beginnings)
        last = max(row["closed"] for row in result["deals"])
        try:
            flows = funding_for_period(
                wallet,
                result["checked"],
                last,
                cache.parent / "verified-funding-v1.sqlite3",
            )
        except FundingHistoryUnavailable:
            # A missing transfer history makes annualization unavailable. Other
            # verified financial results can still be published on schedule.
            result["externalFlows"] = None
        else:
            result["externalFlows"] = [
                flow for flow in flows if first < flow["at"] <= last
            ]
    return {**result, "tag": tag, "commit": commit, "tagActive": start is not None}


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
