from decimal import Decimal

import pytest

from tools.deal_history import reconstruct
from tools.settlement import Settlement
from tools.snapshot import build


def leg(token, qty, entry, mark, cost, realized="0", status="OPEN"):
    return {
        "condition_id": "market",
        "token_id": token,
        "opposite_token_id": "no" if token == "yes" else "yes",
        "outcome": token,
        "outcome_index": int(token == "no"),
        "title": "A market",
        "status": status,
        "current_size": Decimal(qty),
        "total_size": Decimal(10),
        "avg_price": Decimal(entry),
        "current_price": Decimal(mark) if mark is not None else None,
        "total_cost_usdc": Decimal(cost),
        "realized_pnl": Decimal(realized),
        "first_entry_at": 100,
        "last_event_at": 150,
    }


def snapshot(*legs, settlement=None):
    entries = {("market", r["token_id"]): 10_000_000 for r in legs}
    return build(
        legs,
        "Ranked buy",
        90,
        entries,
        entries,
        300,
        settlements={"market": settlement},
    )


@pytest.mark.parametrize(
    "price,loss", [("0.005", -49500), ("0.00501", None), ("0", -50000), (None, None)]
)
def test_threshold_is_relative_to_entry_inclusive_and_uses_remaining_quote_value(
    price, loss
):
    data = snapshot(leg("yes", "10", "0.5", price, "5"))
    assert data["deals"] == []
    assert [r["pnl"] for r in data["provisional"]] == ([] if loss is None else [loss])
    if loss is not None:
        r = data["provisional"][0]
        assert r["markedAt"] == 300 and "closed" not in r and r["method"]
        assert r["priceRule"] == {"outcome": "yes", "entry": 0.5, "mark": float(price)}


def test_profitable_filled_hedge_exempt_even_with_inconsistent_marks():
    crash = leg("yes", "10", ".6", ".001", "6")
    hedge = leg("no", "10", ".3", ".12", "3")
    assert snapshot(crash, hedge)["provisional"] == []
    # Balanced sets worth $10 can still lock in a loss after costs.
    expensive = {**hedge, "total_cost_usdc": Decimal("4.25")}
    assert snapshot(crash, expensive)["provisional"][0]["pnl"] == -2500


def test_partial_hedge_marks_only_excess_and_credits_realized_cash():
    crash = leg("yes", "10", ".5", ".005", "5")
    partial = leg("no", "2", ".2", ".99", ".4", "0.25")
    assert snapshot(crash, partial)["provisional"][0]["pnl"] == -31100
    closed_opposite = leg("no", "0", ".2", ".99", "0", "5", "CLOSED")
    assert snapshot(crash, closed_opposite)["provisional"] == []


def test_unfilled_opposite_and_dust_do_not_create_hedge_or_false_loss():
    crash = leg("yes", "10", ".5", ".005", "5")
    unfilled = leg("no", "0", ".2", ".99", "0")
    assert snapshot(crash, unfilled)["provisional"][0]["pnl"] == -49500
    dust = leg("yes", ".009", ".5", "0", ".0045")
    data = snapshot(dust)
    assert not data["provisional"] and data["deals"][0]["dust"]


def test_recovery_and_settlement_replace_the_provisional_classification():
    crash = leg("yes", "10", ".5", ".005", "5")
    assert len(snapshot(crash)["provisional"]) == 1
    recovered = snapshot({**crash, "current_price": Decimal(".03")})
    assert not recovered["provisional"] and not recovered["deals"]
    final = snapshot(
        crash, settlement=Settlement(250, {"yes": Decimal(0), "no": Decimal(1)})
    )
    assert not final["provisional"] and [r["pnl"] for r in final["deals"]] == [-50000]


def test_open_history_covers_observation_time():
    crash = leg("yes", "10", ".5", ".005", "5")
    buy = {
        "timestamp": 100,
        "type": "TRADE",
        "side": "BUY",
        "token_id": "yes",
        "size": 10,
        "usdc_size": 5,
        "price": Decimal(".5"),
    }
    h = reconstruct(
        [crash],
        [buy],
        lambda *_: [(150, 0.4), (250, 0.005)],
        marked_at=300,
        final_units=-49500,
    )
    assert h["mood"] == "losing" and h["coverage"] == "full"
    assert h["segments"][0][-1] == [199, -49500]
