"""Transport recovery preserves every attempt and never bypasses source stops."""
from datetime import date
from http.client import RemoteDisconnected
from io import BytesIO
import json
import ssl
from urllib.error import HTTPError, URLError

import pytest

from ashare_daily.providers.exchange_universe import SZSE_ENDPOINT, WebsiteTransport


class Clock:
    value = 100.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Response(BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}


def setup_transport(tmp_path, monkeypatch, outcomes, **kwargs):
    clock = Clock()
    monkeypatch.setattr("ashare_daily.providers.exchange_universe.time.monotonic", clock.monotonic)
    monkeypatch.setattr("ashare_daily.providers.exchange_universe.time.sleep", clock.sleep)
    transport = WebsiteTransport(tmp_path, max_attempts=kwargs.pop("max_attempts", 2), **kwargs)
    calls = []

    def open_request(request, timeout):
        calls.append((request.full_url, timeout, clock.value))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Response(outcome)

    monkeypatch.setattr(transport.opener, "open", open_request)
    return transport, calls, clock


def fetch(transport, label="page-0060"):
    return transport(SZSE_ENDPOINT, {"PAGENO": 60}, label=label, target=date(2026, 9, 29))


def test_disconnect_recovers_same_page_and_retains_failed_attempt(tmp_path, monkeypatch):
    transport, calls, _ = setup_transport(tmp_path, monkeypatch,
        [RemoteDisconnected("Remote end closed connection without response"), b'{"records": [1]}'])
    result = fetch(transport)
    assert result["ok"] and result["body"] == {"records": [1]}
    assert result["attempts"] == 2 and transport.count == 2
    assert calls[0][0] == calls[1][0]
    assert calls[1][2] - calls[0][2] >= 1
    previous = result["prior_attempts"][0]
    assert previous["status"] == "network_error"
    assert "RemoteDisconnected" in previous["error"]
    from pathlib import Path
    first = json.loads(Path(previous["response_path"]).read_text(encoding="utf-8"))
    last = json.loads(Path(result["response_path"]).read_text(encoding="utf-8"))
    assert not first["ok"] and first["attempts"] == 1
    assert last == result and len(list(tmp_path.glob("*.json"))) == 2


def test_persistent_disconnect_stops_at_configured_attempt_cap(tmp_path, monkeypatch):
    transport, calls, _ = setup_transport(tmp_path, monkeypatch,
        [RemoteDisconnected("closed"), RemoteDisconnected("closed")])
    result = fetch(transport)
    assert not result["ok"] and result["status"] == "network_error"
    assert result["attempts"] == len(calls) == 2


@pytest.mark.parametrize("status", [401, 403, 429])
def test_access_or_rate_rejection_stops_host_without_retry(tmp_path, monkeypatch, status):
    transport, calls, _ = setup_transport(tmp_path, monkeypatch,
        [HTTPError(SZSE_ENDPOINT, status, "blocked", {}, BytesIO(b"blocked"))])
    result = fetch(transport)
    assert result["http_status"] == status and result["attempts"] == 1
    assert fetch(transport, "next-page")["status"] == "source_stopped"
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", [
    b"not JSON", ssl.SSLCertVerificationError("invalid certificate"),
    URLError(ssl.SSLCertVerificationError("invalid certificate")),
    HTTPError(SZSE_ENDPOINT, 500, "upstream error", {}, BytesIO(b"error")),
])
def test_non_transient_errors_are_not_retried(tmp_path, monkeypatch, outcome):
    transport, calls, _ = setup_transport(tmp_path, monkeypatch, [outcome])
    assert not fetch(transport)["ok"]
    assert len(calls) == 1


def test_run_wide_extra_request_cap_does_not_reset_per_page(tmp_path, monkeypatch):
    outcomes = [value for _ in range(3) for value in (RemoteDisconnected("closed"), b"{}")]
    outcomes.append(RemoteDisconnected("closed"))
    transport, calls, _ = setup_transport(tmp_path, monkeypatch, outcomes)
    results = [fetch(transport, f"page-{number}") for number in range(4)]
    assert [r["ok"] for r in results] == [True, True, True, False]
    assert len(calls) == 7


def test_retry_will_not_start_after_discovery_deadline(tmp_path, monkeypatch):
    transport, calls, _ = setup_transport(tmp_path, monkeypatch,
        [RemoteDisconnected("closed")], deadline_monotonic=100.5)
    assert not fetch(transport)["ok"]
    assert len(calls) == 1
    assert calls[0][1] <= 0.5


def test_spacing_oversleep_cannot_send_after_deadline(tmp_path, monkeypatch):
    transport, calls, clock = setup_transport(tmp_path, monkeypatch,
        [b"{}"], deadline_monotonic=100.4)
    transport.last_request = 99.8
    def delayed_wakeup(seconds):
        clock.value += seconds + 0.3
    monkeypatch.setattr("ashare_daily.providers.exchange_universe.time.sleep", delayed_wakeup)
    result = fetch(transport)
    assert result["status"] == "discovery_elapsed_budget_reached"
    assert result["attempts"] == 0 and transport.count == 0
    assert calls == []


def test_one_attempt_config_keeps_single_request(tmp_path, monkeypatch):
    transport, calls, _ = setup_transport(tmp_path, monkeypatch, [RemoteDisconnected("closed")], max_attempts=1)
    assert fetch(transport)["attempts"] == len(calls) == 1


@pytest.mark.parametrize("attempts", [0, 3, True, 1.5])
def test_attempt_limit_validation(tmp_path, attempts):
    with pytest.raises(ValueError):
        WebsiteTransport(tmp_path, max_attempts=attempts)


@pytest.mark.parametrize("use_cache", [False, True])
@pytest.mark.parametrize("attempts", [1, 2])
def test_discovery_recovers_page_without_duplicating_records(tmp_path, monkeypatch, use_cache, attempts):
    from datetime import datetime
    from urllib.parse import parse_qs, urlsplit
    from ashare_daily.market_schemas import SHANGHAI
    from ashare_daily.providers.exchange_universe import discover_exchange_lists
    from test_f11_exchange_universe import approved, szse_body, szse_raw

    clock = Clock()
    monkeypatch.setattr("ashare_daily.providers.exchange_universe.time.monotonic", clock.monotonic)
    monkeypatch.setattr("ashare_daily.providers.exchange_universe.time.sleep", clock.sleep)
    today = datetime.now(SHANGHAI).date()
    called = []

    class Opener:
        def open(self, request, timeout):
            number = int(parse_qs(urlsplit(request.full_url).query)["PAGENO"][0])
            called.append(number)
            if called == [1, 2]:
                raise RemoteDisconnected("Remote end closed connection without response")
            rows = ([szse_raw(f"{800000+i:06d}") for i in range(20)] if number == 1
                    else [szse_raw("800020", "创业板")])
            body = szse_body(rows, page=number, pages=2, total=21, source_date=today.isoformat())
            return Response(json.dumps(body).encode())

    monkeypatch.setattr("ashare_daily.providers.exchange_universe.build_opener", lambda *a: Opener())
    pages, manifests, requests = discover_exchange_lists(target=today,
        directory=tmp_path / "isolated_test", permissions=approved("SZSE"), max_attempts=attempts,
        cache_directory=tmp_path / "test_cache" if use_cache else None)
    manifest = next(m for m in manifests if m["provider"] == "szse")
    assert manifest["complete"] is (attempts == 2)
    assert called == ([1, 2, 2] if attempts == 2 else [1, 2])
    assert sum(len(page["records"]) for page in pages) == (21 if attempts == 2 else 20)
    assert requests[-1]["attempts"] == attempts
    if attempts == 2:
        assert len(requests[-1]["prior_attempts"]) == 1
        assert pages[-1]["terminal"]
