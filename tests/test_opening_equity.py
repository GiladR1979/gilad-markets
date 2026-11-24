import json
import sqlite3

from tools.opening_equity import attach_opening_equity


def test_uses_only_verified_same_wallet_equity_available_before_first_entry(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE rn2_actions(prepared_at REAL, snapshot TEXT, action TEXT)"
        )

        def capture(at, value, **overrides):
            snapshot = {
                "wallet": "account",
                "snapshot_id": "bound",
                "complete": True,
                "equity": value,
                "cash": 2,
                "evidence": [{"requested_at": at - 2, "received_at": at}],
                **overrides,
            }
            db.execute(
                "INSERT INTO rn2_actions VALUES(?,?,?)",
                (at + 1, json.dumps(snapshot), json.dumps({"snapshot_id": "bound"})),
            )

        capture(98.5, 37_783_382)
        capture(99, 876543210, wallet="another")
        capture(99, 876543210, complete=False)
        capture(99, 876543210, snapshot_id="mismatched")
        capture(99, 876543210, evidence=[])
        capture(99, 876543210, evidence=[{"requested_at": 99, "received_at": 111}])
        capture(100.1, 38_200_000)
        capture(105, 40_000_000)
        capture(105, 60_000_000)
        capture(112, 0)

    deals = [{"history": {"opened": t}} for t in (90, 100, 101, 108, 115)]
    attach_opening_equity(path, "ACCOUNT", deals)
    assert [r["history"].get("openingEquity") for r in deals] == [
        None,
        {"at": 99, "value": 377834},
        {"at": 101, "value": 382000},
        None,
        {"at": 112, "value": 0},
    ]


def test_archived_checkpoint_is_bound_to_wallet_and_the_single_verified_entry(tmp_path):
    ledger = tmp_path / "ledger.sqlite"
    with sqlite3.connect(ledger) as db:
        db.execute(
            "CREATE TABLE rn2_actions(prepared_at REAL, snapshot TEXT, action TEXT)"
        )
    path = tmp_path / "initial.json"
    point = dict(
        source="polygon-archive-before-first-fill",
        wallet="owner",
        block=500,
        at=99,
        firstEntry=100,
        equityAtoms=79_836_336,
    )

    def attach(body):
        path.write_text(json.dumps(body))
        rows = [{"history": {"opened": t}} for t in (90, 100, 101)]
        attach_opening_equity(ledger, "OWNER", rows, path)
        return [r["history"].get("openingEquity") for r in rows]

    assert attach(point) == [None, {"at": 99, "value": 798363}, None]
    assert attach({**point, "wallet": "someone else"}) == [None] * 3
    assert attach({**point, "at": 101}) == [None] * 3
