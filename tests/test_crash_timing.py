from decimal import Decimal

from tools.deal_history import Inventory, first_crash, reconstruct, retain_crash_times


def holding(at, base, token, qty, entries=None, fills=None, sells=None):
    return Inventory(
        at,
        Decimal(base),
        token,
        Decimal(qty),
        {k: Decimal(v) for k, v in (fills or {}).items()},
        {k: Decimal(v) for k, v in (entries or {}).items()},
        {k: Decimal(v) for k, v in (sells or {}).items()},
    )


def test_trigger_uses_actual_entry_at_the_time_and_uncompressed_quotes():
    leg = {
        "token_id": "a",
        "total_size": Decimal(20),
        "current_size": Decimal(0),
        "realized_pnl": Decimal("-5.98"),
        "first_entry_at": 100,
        "last_event_at": 400,
    }

    def fill(t, side, size, price):
        p = Decimal(price)
        return {
            "timestamp": t,
            "type": "TRADE",
            "side": side,
            "size": size,
            "price": p,
            "usdc_size": size * p,
            "token_id": "a",
        }

    events = [
        fill(100, "BUY", 10, ".10"),
        fill(200, "BUY", 10, ".50"),
        fill(400, "SELL", 20, ".001"),
    ]
    tape = [
        (90, 0.0),
        (120, 0.002),
        (199, 0.01),
        (201, 0.0031),
        (237, 0.003),
        (240, 0.20),
        (399, 0.002),
    ]
    # The first lot lost only 98% at t=120. After the second fill, entry is 0.30.
    h = reconstruct([leg], events, lambda *_: tape, final_units=-59800)
    assert h["crashAt"] == 237
    assert h["crashPnl"] == -59400


def test_profitable_complete_set_and_earlier_unhedged_loss():
    hedged = holding(120, "1", "a", "0", {"a": ".5", "b": ".4"})
    end = holding(170, "0", "a", "0")
    tape = lambda token: (
        ([100, 126, 150], [0.5, 0.0005, 0.0005])
        if token == "a"
        else ([100, 126], [0.4, 0.9995])
    )
    assert first_crash([hedged, end], tape, None) is None
    exposed = holding(90, "-5", "a", "10", {"a": ".5"}, {"a": ".5"})
    assert first_crash([exposed, hedged, end], tape, None) is None
    assert first_crash([exposed, hedged._replace(at=135), end], tape, None) == (
        126,
        -49950,
    )


def test_trigger_on_smaller_leg_checks_the_filled_hedge_at_that_instant():
    position = holding(
        100, "-0.75", "b", "2", {"a": ".4", "b": ".6"}, {"a": ".4", "b": ".6"}
    )
    end = holding(500, "0", "a", "0")
    tape = lambda token: (
        ([150, 410, 460], [0.001, 0.001, 0.001])
        if token == "a"
        else ([150, 455], [0.99, 0.25])
    )
    # Profitable at 150, quote for the exposed side is stale at 410, negative when the hedge reprices at 455.
    assert first_crash([position, end], tape, None) == (455, -2500)


def test_stale_exposed_quote_can_prove_a_loss_without_valuing_it():
    pos = holding(100, "-3", "b", "2", {"a": ".4", "b": ".6"}, {"b": ".6"})
    end = holding(600, "0", "b", "0")
    tape = lambda token: ([500], [0.001]) if token == "a" else ([], [])
    assert first_crash([pos, end], tape, None) == (500, None)
    paired = pos._replace(excess=Decimal(0))
    assert first_crash([paired, end], tape, None) == (500, -30000)


def test_last_sale_can_trigger_but_marks_after_exit_cannot():
    pos = holding(100, "-2", "a", "4", {"a": ".5"}, {"a": ".5"})
    end = holding(150, "0", "a", "0", sells={"a": ".004"})
    tape = lambda token: ([120, 160], [0.4, 0.001])
    assert first_crash([pos, end], tape, None) == (150, -19840)
    assert first_crash([pos, end._replace(sells={})], tape, None) is None


def test_first_live_observation_survives_refresh_and_final_close(tmp_path):
    path = tmp_path / "crossings.sqlite3"

    def publish(pnl, opened, **fields):
        h = {"opened": opened}
        if "historical" in fields:
            h["crashAt"] = fields.pop("historical")
            h["crashPnl"] = fields.pop("value", None)
        deal = {"pnl": pnl, "history": h, **fields}
        retain_crash_times([([{"condition_id": "m"}], deal, None)], path)
        return deal.get("crashAt"), deal.get("crashPnl")

    assert publish(-100, 100, markedAt=190) == (190, -100)
    assert publish(-104, 100, markedAt=240) == (190, -100)
    assert publish(-105, 100, closed=310, historical=175) == (175, None)
    assert publish(-99, 100, markedAt=320) == (175, None)
    assert publish(-105, 100, closed=330, historical=175, value=-93) == (175, -93)
    assert publish(-105, 100, closed=330) == (175, -93)
    assert publish(100, 100, closed=330) == (None, None)
    assert publish(-100, 400, closed=500) == (None, None)
