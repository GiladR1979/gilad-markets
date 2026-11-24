from decimal import Decimal
from urllib.error import URLError

import pytest

from tools import bankroll_flows as flows


WALLET = "0x" + "a" * 40


def flow(tx=1, at=5, kind="DEPOSIT", amount="1.00", **extra):
    return dict(
        proxy_wallet=WALLET,
        transaction_hash="0x" + f"{tx:064x}",
        timestamp=at,
        type=kind,
        usdc_size=Decimal(amount),
        **extra,
    )


def page(rows, cursor=None):
    return {
        "data": rows,
        "pagination": {"has_more": cursor is not None, "next_cursor": cursor},
    }


def test_groups_exact_decimals_across_pages_and_returns_only_public_fields(monkeypatch):
    first = flow(amount="0.123449")
    pages = [
        page([flow(at=8, kind="WITHDRAWAL", tx=3, amount="2"), first], "next"),
        page([first, flow(tx=2, amount="0.000049"), flow(tx=4, at=11)]),
    ]
    requests = []

    def get(url, params):
        requests.append((url, dict(params)))
        return pages[len(requests) - 1]

    monkeypatch.setattr(flows, "get_json", get)
    assert flows.fetch_external_flows(WALLET, 10) == [
        {"at": 5, "amount": 1235},
        {"at": 8, "amount": -20000},
    ]
    assert requests[0] == (
        "https://data-api.polymarket.com/v2/activity",
        {
            "user": WALLET,
            "type": "DEPOSIT,WITHDRAWAL",
            "exclude_deposits_withdrawals": "false",
            "start": 1,
            "end": 10,
            "limit": 1000,
        },
    )
    assert requests[1][1] == {**requests[0][1], "cursor": "next"}


def test_same_transaction_and_second_keep_distinguishable_flows(monkeypatch):
    rows = [
        flow(amount="1.25"),
        flow(kind="WITHDRAWAL", amount="1.25"),
        flow(tx=2, at=10, amount="2.000049", log_index=1),
        flow(tx=2, at=10, amount="2.000049", log_index=2),
    ]
    monkeypatch.setattr(flows, "get_json", lambda *_: page(rows))
    assert flows.fetch_external_flows(WALLET, 10) == [{"at": 10, "amount": 40001}]


@pytest.mark.parametrize(
    "replacement",
    [
        {"usdc_size": Decimal("2")},
        {"timestamp": 6},
        {"log_index": 3},
    ],
)
def test_conflicting_duplicates_do_not_silently_corrupt_totals(
    monkeypatch, replacement
):
    row = flow()
    monkeypatch.setattr(
        flows, "get_json", lambda *_: page([row, {**row, **replacement}])
    )
    with pytest.raises(flows.FundingHistoryUnavailable):
        flows.fetch_external_flows(WALLET, 10)


@pytest.mark.parametrize(
    "change",
    [
        {"type": "TRADE"},
        {"type": "PREMATURE_WITHDRAWAL"},
        {"proxy_wallet": "0x" + "b" * 40},
        {"proxy_wallet": None},
        {"timestamp": -1},
        {"timestamp": True},
        {"transaction_hash": "missing"},
        {"usdc_size": Decimal("-2")},
        {"usdc_size": Decimal("NaN")},
        {"usdc_size": True},
        {"usdc_size": Decimal("Infinity")},
        {"usdc_size": Decimal("1e14")},
        {"log_index": -1},
    ],
)
def test_rejects_unverified_rows(monkeypatch, change):
    monkeypatch.setattr(flows, "get_json", lambda *_: page([{**flow(), **change}]))
    with pytest.raises(flows.FundingHistoryUnavailable):
        flows.fetch_external_flows(WALLET, 10)


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"pagination": {}, "data": [flow()]},
        {"data": [], "pagination": {"has_more": True, "next_cursor": "cursor"}},
        {"data": [flow()], "pagination": {"has_more": False, "next_cursor": "cursor"}},
        {"data": [flow()], "pagination": {"has_more": True, "next_cursor": None}},
        {"data": [flow()], "pagination": {"has_more": None, "next_cursor": None}},
        page([flow()], "cursor"),
    ],
)
def test_incomplete_pages_raise_even_after_good_rows(monkeypatch, broken):
    pages = iter([page([flow()], "cursor"), broken])
    monkeypatch.setattr(flows, "get_json", lambda *_: next(pages))
    with pytest.raises(flows.FundingHistoryUnavailable):
        flows.fetch_external_flows(WALLET, 10)


@pytest.mark.parametrize("error", [URLError("unavailable"), ValueError("bad JSON")])
def test_read_failure_is_distinct_from_verified_empty_history(monkeypatch, error):
    def fail(*_):
        raise error

    monkeypatch.setattr(flows, "get_json", fail)
    with pytest.raises(flows.FundingHistoryUnavailable):
        flows.fetch_external_flows(WALLET, 10)
    monkeypatch.setattr(flows, "get_json", lambda *_: page([]))
    assert flows.fetch_external_flows(WALLET, 10) == []
