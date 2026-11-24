from collections import Counter
from decimal import Decimal

import pytest

from tools.settlement import Settlement
from tools.snapshot import build, matched, release_tag


def leg(condition, token, pnl, quantity=0, bought=5, entered=120, closed=150):
    return dict(
        condition_id=condition,
        token_id=token,
        outcome_index=0,
        first_entry_at=entered,
        last_event_at=closed,
        total_size=bought,
        current_size=quantity,
        title="A market",
        status="CLOSED",
        realized_pnl=pnl,
    )


def test_all_legs_flat_or_dust_and_only_realized_amounts_count():
    rows = [
        leg("a", "1", "2.5001"),
        leg("a", "2", "-1.5", quantity="0.0099"),
        leg("b", "3", 4),
        leg("b", "4", 7, quantity="0.0100"),
    ]
    sizes = Counter({("a", "1"): 5_000_000, ("a", "2"): 5_000_050})
    data = build(rows, "Ranked buy", 110, sizes, sizes, 200)
    assert data["deals"] == [
        dict(
            market="A market",
            closed=150,
            pnl=10001,
            legs=2,
            method=True,
            verified=True,
            dust=True,
        )
    ]


def test_latest_excludes_carry_in_and_mixed_or_unmatched_purchases():
    rows = [leg("a", "1", -1, entered=90), leg("b", "2", 0), leg("c", "3", 3)]
    sizes = {("a", "1"): 5_000_000, ("b", "2"): 5_000_000}
    latest = {("a", "1"): 5_000_000, ("b", "2"): 2_000_000}
    deals = build(rows, "Ranked buy", 110, sizes, latest, 200)["deals"]
    assert not any(r["method"] for r in deals)
    assert sum(r["verified"] for r in deals) == 2
    assert sum(r["pnl"] for r in deals) == 20_000


def test_share_precision_and_overlapping_views():
    row = leg("a", "1", Decimal("0.0001"), bought="12.7547")
    assert matched(row, {("a", "1"): 12_754_799})
    assert not matched(row, {("a", "1"): 12_754_800})
    assert len(build([row, row], "Ranked buy", 110, {}, {}, 200)["deals"]) == 1
    with pytest.raises(ValueError, match="lifecycle"):
        build([row, {**row, "current_size": 2}], "Ranked buy", 110, {}, {}, 200)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("active_status", ["OPEN", "REDEEMABLE"])
def test_partially_closed_token_appears_in_both_views(reverse, active_status):
    closed = dict(
        leg("a", "1", "3.4784", quantity="0.4538", bought="25.3"),
        avg_price="0.36",
        total_cost_usdc="0.1633",
        unrealized_pnl="0",
    )
    active = dict(
        closed, status=active_status, current_price="0.0255", unrealized_pnl="-0.1518"
    )
    peer = leg("a", "2", "-2.733")
    independent = dict(leg("b", "3", "0.4236"), title="Independent close")
    rows = [active, closed] if reverse else [closed, active]
    rows += [peer, independent]
    result = build(rows, "Tagged", None, {}, {}, 200)
    assert [(r["market"], r["pnl"]) for r in result["deals"]] == [
        ("Independent close", 4236)
    ]
    assert not result["provisional"]
    settled = build(
        rows,
        "Tagged",
        None,
        {},
        {},
        200,
        settlements={"a": Settlement(180, {"1": Decimal(0), "2": Decimal(1)})},
    )
    assert [(r["market"], r["pnl"], r["legs"]) for r in settled["deals"]] == [
        ("A market", 5821, 2),
        ("Independent close", 4236, 1),
    ]


@pytest.mark.parametrize(
    "changed",
    [
        {"condition_id": "other"},
        {"current_size": 1},
        {"realized_pnl": 3},
        {"last_event_at": 155},
    ],
)
def test_distinct_accounting_in_two_views_still_rejects_inconsistent_snapshot(changed):
    row = leg("a", "1", 2, quantity=2)
    with pytest.raises(ValueError, match="lifecycle"):
        build([row, {**row, "status": "OPEN", **changed}], "Tagged", None, {}, {}, 200)


def test_tag_order_uses_release_number_across_pages(monkeypatch):
    import json
    from types import SimpleNamespace

    pages = [
        [{"name": "Successful9", "commit": {"sha": "a" * 40}}],
        [
            {"name": "Successful10", "commit": {"sha": "b" * 40}},
            {"name": "some-other-tag", "commit": {"sha": "c" * 40}},
        ],
    ]
    monkeypatch.setattr(
        "tools.snapshot.subprocess.run",
        lambda *args, **kw: SimpleNamespace(stdout=json.dumps(pages)),
    )
    assert release_tag("owner/repo", "gh") == ("Successful10", "b" * 40)


def test_tag_with_no_rollout_is_pending():
    row = leg("a", "1", 10)
    totals = {("a", "1"): 5_000_000}
    data = build([row], "Tagged release", None, totals, {}, 200)
    assert data["methodSince"] is None
    assert not data["deals"][0]["method"]
