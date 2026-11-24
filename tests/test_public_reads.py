import io
from decimal import Decimal
from urllib.error import HTTPError

import pytest

from tools import deal_history


def test_public_read_retries_a_transient_service_error(monkeypatch):
    attempts = []
    sleeps = []

    def open_read(request, *, timeout):
        attempts.append((request.full_url, request.get_method(), timeout))
        if len(attempts) == 1:
            raise HTTPError(request.full_url, 503, "Unavailable", {}, None)
        return io.BytesIO(b'{"value":0.1}')

    monkeypatch.setattr(deal_history, "urlopen", open_read)
    monkeypatch.setattr(deal_history.time, "sleep", sleeps.append)
    assert deal_history.get_json("https://example.test/data", {"id": 1}) == {
        "value": Decimal("0.1")
    }
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert attempts[0][1] == "GET" and sleeps == [1]


@pytest.mark.parametrize("status,expected", [(400, 1), (503, 3)])
def test_public_read_preserves_failure_after_bounded_retries(
    monkeypatch, status, expected
):
    attempts = []

    def open_read(request, *, timeout):
        attempts.append(1)
        raise HTTPError(request.full_url, status, "Unavailable", {}, None)

    monkeypatch.setattr(deal_history, "urlopen", open_read)
    monkeypatch.setattr(deal_history.time, "sleep", lambda _: None)
    with pytest.raises(HTTPError) as error:
        deal_history.get_json("https://example.test/data", {})
    assert error.value.code == status and len(attempts) == expected
