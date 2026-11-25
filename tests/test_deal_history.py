from decimal import Decimal

import pytest

from tools.deal_history import compress, populate, reconstruct
from tools import publish
from tools.settlement import Settlement


def test_publisher_reads_the_same_snapshot_after_it_outgrows_inline_contents(
    monkeypatch,
):
    blob = {"encoding": "base64", "content": "e30K"}

    def get_blob(exe, endpoint):
        assert endpoint == publish.PUBLIC_REPO + "/git/blobs/abc"
        return blob

    monkeypatch.setattr(publish, "github", get_blob)
    assert publish.snapshot_bytes("gh", {"encoding": "none", "sha": "abc"}) == b"{}\n"
    assert publish.snapshot_bytes("gh", blob) == b"{}\n"


def position(token="a", opposite="b", bought="10", pnl="1", remaining="0", close=300):
    return {
        "first_entry_at": 100,
        "last_event_at": close,
        "token_id": token,
        "opposite_token_id": opposite,
        "total_size": Decimal(bought),
        "current_size": Decimal(remaining),
        "realized_pnl": Decimal(pnl),
    }


def trade(t, side, size, amount, token="a"):
    return {
        "type": "TRADE",
        "timestamp": t,
        "side": side,
        "token_id": token,
        "size": Decimal(size),
        "usdc_size": Decimal(amount),
        "price": Decimal(amount) / Decimal(size),
    }


@pytest.mark.parametrize(
    "low,expected_mood,drop",
    [(0.59, "smooth", 2), (0.55, "troubling", 10), (0.4, "nerve-wracking", 40)],
)
def test_drawdown_uses_change_from_high_over_total_invested(low, expected_mood, drop):
    events = [trade(100, "BUY", "10", "5"), trade(300, "SELL", "10", "6")]
    h = reconstruct([position()], events, lambda *_: [(150, 0.60), (200, low)])
    assert (h["coverage"], h["mood"], h["pullback"]) == ("full", expected_mood, drop)
    assert h["stake"] == 50000
    assert h["segments"][0][0] == [0, 0]
    assert h["segments"][0][-1][0] == 199  # real close is a separate observation


def test_gap_splits_line_and_withholds_positive_mood_but_honors_realized_loss():
    buy = trade(100, "BUY", "10", "5")
    tape = lambda *_: [(120, 0.5), (600, 0.6)]
    for amount, pnl, mood in [("6", "1", "unrated"), ("4", "-1", "losing")]:
        h = reconstruct(
            [position(pnl=pnl, close=700)],
            [buy, trade(700, "SELL", "10", amount)],
            tape,
        )
        assert (h["coverage"], h["mood"]) == ("partial", mood)
        assert len(h["segments"]) == 2
        assert "pullback" not in h


def test_dust_at_later_bookkeeping_close_does_not_extend_market_price_exposure():
    leg = position(remaining=".005", close=1000, pnl=".997")
    h = reconstruct(
        [leg],
        [trade(100, "BUY", "10", "5"), trade(300, "SELL", "9.995", "5.997")],
        lambda *_: [(120, 0.5), (180, 0.6), (240, 0.6)],
    )
    assert h["coverage"] == "full"
    assert (h["economicClose"], h["closeReason"]) == (300, "dust")
    assert h["segments"][0][-1][0] == 199


def redeem(t, size, amount, token="a"):
    return {
        "type": "REDEEM",
        "token_id": token,
        "timestamp": t,
        "size": Decimal(size),
        "usdc_size": Decimal(amount),
    }


def test_dust_stays_closed_at_sale_after_resolution_and_redemption(
    tmp_path, monkeypatch
):
    fills = [
        trade(100, "BUY", "10", "5"),
        trade(200, "SELL", "9.995", "5.997"),
        redeem(400, ".005", ".005"),
    ]
    leg = dict(position(pnl="1.002", close=400), condition_id="m")
    resolved = Settlement(350, {"a": Decimal(1)})
    deal = {"pnl": 10020, "closed": 400, "settled": True}
    monkeypatch.setattr("tools.deal_history.token_prices", lambda *a, **kw: [])
    populate([([leg], deal, resolved)], "", tmp_path, activity={"m": fills})
    assert (deal["closed"], deal["closeReason"], deal["bookedAt"]) == (200, "dust", 400)
    assert "settled" not in deal and "economicClose" not in deal["history"]
    assert deal["pnl"] == 10020


@pytest.mark.parametrize("reopened", [250, 300])
def test_material_reopening_replaces_earlier_exit_and_includes_exact_dust_threshold(
    reopened,
):
    fills = [
        trade(100, "BUY", "10", "5"),
        trade(200, "SELL", "10", "6"),
        trade(reopened, "BUY", ".010", ".005"),
        trade(300, "SELL", ".010", ".006"),
    ]
    h = reconstruct(
        [position(bought="10.010", pnl="1.001", close=400)], fills, lambda *_: []
    )
    assert (h["economicClose"], h["closeReason"]) == (300, "exit")


@pytest.mark.parametrize("resolution, expected", [(200, 200), (350, 300)])
def test_redemption_and_confirmed_payout_use_first_evidence_of_settlement(
    resolution, expected
):
    fills = [trade(100, "BUY", "10", "5"), redeem(300, "10", "0")]
    h = reconstruct(
        [position(pnl="-5", close=300)],
        fills,
        lambda *_: [(110, 0.4), (150, 0.2)],
        settlement=Settlement(resolution, {"a": Decimal(0)}),
        final_units=-50000,
    )
    assert (h["economicClose"], h["closeReason"]) == (expected, "settlement")
    assert (h["crashAt"], h["crashPnl"]) == (expected, -50000)
    assert h["segments"][0][-1][0] < expected - h["opened"]


def test_official_stamp_cannot_end_another_material_entry():
    fills = [
        trade(100, "BUY", "2", "1"),
        trade(150, "SELL", "2", "1"),
        trade(250, "BUY", "2", "1"),
        redeem(300, "2", "0"),
    ]
    h = reconstruct(
        [position(bought="4", pnl="-1")],
        fills,
        lambda *_: [],
        settlement=Settlement(200, {"a": Decimal(0)}),
        final_units=-10000,
    )
    assert (h["economicClose"], h["closeReason"], h["crashAt"]) == (
        300, "settlement", 300
    )


def test_unmatched_economic_events_fall_back_to_the_known_close():
    leg = position(bought="20", pnl="-1")
    h = reconstruct(
        [leg],
        [trade(100, "BUY", "10", "5"), trade(300, "SELL", "10", "4")],
        lambda *_: [],
    )
    assert (h["coverage"], h["mood"], h["segments"]) == ("close", "losing", [])
    assert "economicClose" not in h and "closeReason" not in h


def test_hedged_interval_values_complete_sets_at_par():
    a = position(pnl="1")
    b = {**position("b", "a", pnl="0"), "first_entry_at": 200}
    events = [
        trade(100, "BUY", "10", "5"),
        trade(200, "BUY", "10", "4", "b"),
        {
            "type": "MERGE",
            "timestamp": 300,
            "token_id": None,
            "usdc_size": 10,
            "size": 10,
        },
    ]

    def tape(token, *_):
        assert token == "a"
        return [(130, 0.5), (160, 0.55)]

    h = reconstruct([a, b], events, tape)
    assert h["coverage"] == "full" and h["stake"] == 90000
    assert h["segments"][0][-1] == [199, 10000]


def test_compression_keeps_bucket_extremes_and_time_order():
    vals = [(i, 100 if i == 72 else -25 if i == 73 else 0) for i in range(1000)]
    result = compress(vals, 0, 1000)
    assert result == sorted(result)
    assert [72, 1000000] in result and [73, -250000] in result
    assert result[0] == [0, 0] and result[-1] == [999, 0]
