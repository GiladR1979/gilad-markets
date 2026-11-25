import json
from decimal import Decimal
from urllib.error import URLError

import pytest

from tools.deal_history import reconstruct
from tools.settlement import Settlement, confirm, lookup
from tools.snapshot import build


def records():
    book = {
        "condition_id": "m",
        "closed": True,
        "tokens": [
            {"token_id": "over", "price": 1, "winner": True},
            {"token_id": "under", "price": 0, "winner": False},
        ],
    }
    market = {
        "conditionId": "m",
        "closed": True,
        "umaResolutionStatus": "resolved",
        "closedTime": "2026-11-21 02:14:08.926103+00",
        "clobTokenIds": json.dumps(["under", "over"]),
        "outcomePrices": json.dumps(["0", "1"]),
    }
    return book, market


def test_final_payout_matches_token_identity_across_both_sources():
    book, market = records()
    settlement = confirm("m", book, [market])
    assert settlement.payouts == {"under": 0, "over": 1}
    assert settlement.closed == 1795227248
    assert confirm("m", book, [{**market, "umaResolutionStatus": "disputed"}]) is None
    assert confirm("m", {**book, "closed": False}, [market]) is None
    assert confirm("m", book, [{**market, "outcomePrices": '["1","0"]'}]) is None
    assert confirm("m", book, [{**market, "conditionId": "another"}]) is None
    book["tokens"][0]["winner"] = False
    assert confirm("m", book, [market]) is None


def test_equal_payout_needs_explicit_resolution():
    book, market = records()
    for token in book["tokens"]:
        token.update(price=Decimal("0.5"), winner=False)
    market["outcomePrices"] = '["0.5","0.5"]'
    assert set(confirm("m", book, [market]).payouts.values()) == {Decimal("0.5")}
    assert confirm("m", book, [{**market, "umaResolutionStatus": None}]) is None


def test_material_redemption_looks_up_final_time_after_balance_is_zero(tmp_path, monkeypatch):
    condition = "0x" + "a" * 64
    row = {"condition_id": condition, "current_size": 0, "status": "CLOSED"}
    activity = {condition: [{"type": "REDEEM", "size": Decimal("1.5")}]}
    book, market = records()
    book["condition_id"] = market["conditionId"] = condition
    calls = []

    def fetch(url, params):
        calls.append(url)
        return [market] if params else book

    monkeypatch.setattr("tools.settlement.get_json", fetch)
    cache = tmp_path / "confirmed.sqlite3"
    first = lookup([row], Decimal(".01"), activity=activity, cache_path=cache)
    assert first[condition].closed == 1795227248
    assert lookup([row], Decimal(".01"), activity=activity, cache_path=cache) == first
    assert len(calls) == 2


def test_unconfirmed_historical_timing_is_retried_and_does_not_hide_other_results(monkeypatch):
    condition = "0x" + "b" * 64
    row = {"condition_id": condition, "current_size": 0, "status": "CLOSED"}
    activity = {condition: [{"type": "MERGE", "size": Decimal("2")}]}

    def offline(*args):
        raise URLError("temporarily unavailable")

    monkeypatch.setattr("tools.settlement.get_json", offline)
    assert lookup([row], Decimal(".01"), activity=activity) == {}
    with pytest.raises(URLError):
        lookup([{**row, "current_size": 2, "status": "REDEEMABLE"}], Decimal(".01"))


def leg(token, status, size, realized, cost):
    return {
        "condition_id": "m",
        "token_id": token,
        "outcome_index": int(token == "over"),
        "title": "Example",
        "status": status,
        "current_size": Decimal(size),
        "total_size": Decimal(3),
        "first_entry_at": 100,
        "last_event_at": 150,
        "realized_pnl": Decimal(realized),
        "current_price": Decimal(0),
        "total_cost_usdc": Decimal(cost),
    }


def deals(legs, confirmed=True):
    settlement = Settlement(200, {"under": Decimal(0), "over": Decimal(1)})
    return build(
        legs,
        "test",
        90,
        {},
        {},
        300,
        settlements={"m": settlement} if confirmed else {},
    )["deals"]


def test_loss_counted_once_before_and_after_redeeming_and_while_quote_is_zero():
    pending = leg("under", "REDEEMABLE", "3", "0.23", "1.20")
    assert deals([pending], False) == []
    closed = deals([pending])[0]
    assert (
        closed["pnl"] == -9700
        and closed["settled"]
        and closed["closed"] == 200
        and not closed["dust"]
    )
    redeemed = {
        **pending,
        "current_size": 0,
        "status": "CLOSED",
        "realized_pnl": "-0.97",
        "last_event_at": 220,
    }
    rows = deals([redeemed], False)
    assert len(rows) == 1 and rows[0]["pnl"] == closed["pnl"]


def test_closed_residual_is_already_booked_and_opposite_winner_receives_final_payout():
    loss = leg("under", "CLOSED", "0.0115", "-0.50", "0.0057")
    win = leg("over", "REDEEMABLE", "3", "0.10", "1.50")
    assert deals([loss, win])[0]["pnl"] == 11000


def test_later_resolution_does_not_move_a_flat_market_without_activity_evidence():
    result = deals([leg("under", "CLOSED", "0", "0.67", "0")])[0]
    assert result["closed"] == 150
    assert result["pnl"] == 6700
    assert "settled" not in result


def test_settlement_extends_graph_beyond_last_trade_and_sets_losing_mood():
    position = leg("under", "REDEEMABLE", "3", "0", "1.20")
    trade = {
        "timestamp": 100,
        "type": "TRADE",
        "side": "BUY",
        "token_id": "under",
        "size": 3,
        "usdc_size": Decimal("1.2"),
        "price": Decimal("0.4"),
    }
    h = reconstruct(
        [position],
        [trade],
        lambda *_: [(130, 0.3), (180, 0.05)],
        settlement=Settlement(200, {"under": Decimal(0), "over": Decimal(1)}),
        final_units=-12000,
    )
    assert h["coverage"] == "full" and h["mood"] == "losing"
    assert h["segments"][0][-1] == [99, -10500]
