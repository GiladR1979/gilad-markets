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


def simulate_outage(*_):
    raise flows.FundingHistoryUnavailable("Activity provider unavailable")


@pytest.mark.parametrize(
    "records", [[], [{"at": 8, "amount": -12}, {"at": 10, "amount": 123}]]
)
def test_outage_uses_verified_coverage_only_for_the_right_wallet(
    tmp_path, monkeypatch, records
):
    cache = tmp_path / "private" / "funding.sqlite3"
    monkeypatch.setattr(flows, "fetch_external_flows", lambda *_: records)
    earlier = [r for r in records if r["at"] <= 8]
    assert flows.funding_for_period(WALLET, 10, 8, cache) == earlier
    monkeypatch.setattr(flows, "fetch_external_flows", simulate_outage)
    assert flows.funding_for_period(WALLET.upper(), 20, 8, cache) == earlier
    assert flows.funding_for_period(WALLET, 20, 10, cache) == records
    with pytest.raises(flows.FundingHistoryUnavailable):
        flows.funding_for_period(WALLET, 20, 11, cache)
    with pytest.raises(flows.FundingHistoryUnavailable):
        flows.funding_for_period("0x" + "b" * 40, 20, 8, cache)


def test_fresh_history_can_correct_or_remove_old_transfers(tmp_path, monkeypatch):
    cache = tmp_path / "funding.sqlite3"
    old = [{"at": 5, "amount": 125}, {"at": 6, "amount": 13}]
    revised = [{"at": 5, "amount": 123}, {"at": 20, "amount": 55}]
    monkeypatch.setattr(
        flows,
        "fetch_external_flows",
        lambda wallet, checked: old if checked == 12 else revised,
    )
    assert flows.funding_for_period(WALLET, 12, 10, cache) == old
    assert flows.funding_for_period(WALLET, 21, 15, cache) == revised[:1]
    monkeypatch.setattr(flows, "fetch_external_flows", simulate_outage)
    assert flows.funding_for_period(WALLET, 22, 21, cache) == revised


def test_fresh_but_older_request_cannot_overwrite_newer_coverage(tmp_path, monkeypatch):
    cache = tmp_path / "funding.sqlite3"
    new = [{"at": 5, "amount": 5}, {"at": 20, "amount": 20}]
    old = [{"at": 5, "amount": 2}]
    monkeypatch.setattr(
        flows, "fetch_external_flows", lambda w, t: new if t == 21 else old
    )
    assert flows.funding_for_period(WALLET, 21, 21, cache) == new
    # An older request reaches the cache after a newer request already committed.
    assert flows.funding_for_period(WALLET, 10, 9, cache) == new[:1]
    monkeypatch.setattr(flows, "fetch_external_flows", simulate_outage)
    assert flows.funding_for_period(WALLET, 30, 21, cache) == new


def test_unusable_cache_cannot_hide_a_verified_live_result_or_fabricate_one(
    tmp_path, monkeypatch
):
    cache = tmp_path / "funding.sqlite3"
    cache.write_bytes(b"invalid cache")
    monkeypatch.setattr(
        flows, "fetch_external_flows", lambda *_: [{"at": 5, "amount": 1}]
    )
    assert flows.funding_for_period(WALLET, 10, 10, cache) == [{"at": 5, "amount": 1}]
    monkeypatch.setattr(flows, "fetch_external_flows", simulate_outage)
    with pytest.raises(flows.FundingHistoryUnavailable):
        flows.funding_for_period(WALLET, 11, 10, cache)
