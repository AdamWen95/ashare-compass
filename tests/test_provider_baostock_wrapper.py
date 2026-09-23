"""Offline wrapper contracts: no public SDK requests or research writes."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from ashare_daily.providers.base import DailyBarRequest, SecurityIdentity
from ashare_daily.providers.baostock import base_result, expected_fields, raw_hash, validate_request
from ashare_daily.providers.baostock_f2 import BaoStockF2Client
from ashare_daily.providers.baostock_provider import BaoStockProvider


DAY = "2026-09-10"
STAMP = "2026-09-10T21:00:00+08:00"
IDENTITY = SecurityIdentity("synthetic-source-id", "688001", "SSE", "star", metadata_verified=True)


def request(identity=IDENTITY, *, mode="unadjusted", dates=(DAY,), start=DAY, end=DAY):
    return DailyBarRequest(identity, start, end, dates, mode)


def row(**updates):
    return {"date": DAY, "code": IDENTITY.symbol, "open": "10", "high": "11", "low": "9",
            "close": "10", "preclose": "10", "volume": "12345", "amount": "123450",
            "adjustflag": "3", "tradestatus": "1", "isST": "0", **updates}


def reply(req=None, rows=None, *, error=None, attempts=1, operation="history_f2", parameters=None):
    parameters = parameters or (req or request()).parameters()
    parameters = validate_request(operation, parameters)
    data = [row()] if rows is None else rows
    value = base_result(operation, parameters)
    value.update(ok=True, status="ok" if data else "empty_confirmed", error_code="0", error_msg="success",
                 fields=expected_fields(operation, parameters), rows=data, fetched_at=STAMP,
                 login={"ok": True, "error_code": "0", "error_msg": "success"},
                 provenance_mode="offline_test", sdk_log="original bounded log",
                 session={"login_reused": True, "login_at": STAMP},
                 diagnostics={"failure_stage": None, "request_id": 9, "events": []}, elapsed_seconds=2.75)
    if error:
        value.update(ok=False, status=error[0], error_code=error[1], error_msg="original source failure", rows=[])
        value["diagnostics"]["failure_stage"] = "login_wait"
    value["raw_hash"] = raw_hash(value["fields"], value["rows"])
    value["attempts"] = [{"attempt": i + 1, "status": value["status"], "login": dict(value["login"])}
                         for i in range(attempts)]
    return value


class FakeClient:
    def __init__(self, response):
        self.response, self.calls, self.close_count = response, [], 0

    def query(self, operation, **parameters):
        self.calls.append((operation, parameters))
        return self.response

    def close(self):
        self.close_count += 1


def provider(response):
    client = FakeClient(response)
    return BaoStockProvider(client, mode="offline_test"), client


def test_success_retains_original_response_login_diagnostics_and_both_hash_meanings():
    original = reply(attempts=2)
    before = deepcopy(original)
    encoded = json.dumps(original, ensure_ascii=False, sort_keys=True).encode()
    wrapper, client = provider(original)
    result = wrapper.fetch_daily_bars(request())
    assert result.response is original and original == before
    assert sha256(json.dumps(original, ensure_ascii=False, sort_keys=True).encode()).hexdigest() == sha256(encoded).hexdigest()
    assert original["raw_hash"] == raw_hash(original["fields"], original["rows"])
    assert original["raw_hash"] != sha256(encoded).hexdigest()
    assert result.status == "success" and result.usable and result.has_facts
    assert result.records[0]["volume_shares"] == 12345 and result.records[0]["amount_cny"] == "123450"
    assert result.response["login"] == before["login"] and result.response["session"] == before["session"]
    assert result.response["diagnostics"] == before["diagnostics"]
    assert result.source_business_date is None and result.fetched_at == STAMP
    assert result.source_endpoint == "baostock://public-api.baostock.com:10030/query_history_k_data_plus"
    assert result.metrics["requests"] == 2 and result.metrics["retries"] == 1 and result.metrics["elapsed_seconds"] == 2.75
    assert result.metrics["verification_kind"] == "offline_test"
    assert client.calls == [("history_f2", request().parameters())]


@pytest.mark.parametrize("status,code", [("timeout", "worker_timeout"), ("unknown", "10002007"),
    ("permission_denied", "10001006"), ("rate_limited", "10001005"), ("schema_changed", "worker_protocol_error")])
def test_source_errors_are_preserved_without_wrapper_retry(status, code):
    original = reply(error=(status, code), attempts=2)
    wrapper, client = provider(original)
    result = wrapper.fetch_daily_bars(request())
    assert len(client.calls) == 1 and result.status == status
    assert result.response is original and result.response["error_code"] == code
    assert result.response["diagnostics"]["failure_stage"] == "login_wait"
    assert result.metrics["requests"] == 2 and result.metrics["retries"] == 1
    assert not result.usable and not result.records


def test_latched_source_reports_zero_attempts_and_no_added_retry():
    original = reply(error=("rate_limited", "10001005"), attempts=0)
    original["source_stopped"] = True
    wrapper, client = provider(original)
    result = wrapper.fetch_daily_bars(request())
    assert len(client.calls) == 1 and result.response["source_stopped"] is True
    assert result.metrics["requests"] == result.metrics["retries"] == 0


@pytest.mark.parametrize("rows,expected_status", [([], "partial"), ([row(tradestatus="", isST="")], "partial"),
    ([row(isST="")], "success"), ([row(tradestatus="0", volume="", amount="")], "success")])
def test_empty_unknown_and_suspended_keep_existing_quality_meanings(rows, expected_status):
    wrapper, _ = provider(reply(rows=rows))
    result = wrapper.fetch_daily_bars(request())
    assert result.status == expected_status
    assert result.usable is (expected_status == "success")
    assert result.quality["complete"] is False
    if not rows:
        assert result.response["status"] == "empty_confirmed" and result.quality["missing_dates"] == [DAY]
    elif rows[0]["tradestatus"] == "":
        assert result.records[0]["tradestatus"] is None and result.records[0]["is_st"] is None
        assert result.quality["trading_status_unknown_dates"] == [DAY]
    elif rows[0]["tradestatus"] == "0":
        assert result.records[0]["volume_shares"] is None
        assert "missing_volume_shares" in result.records[0]["quality_flags"]


def test_forward_adjusted_is_one_complete_original_window():
    req = request(mode="forward_adjusted", dates=("2025-05-26", DAY), start="2025-05-26")
    original = reply(req, [row(date="2025-05-26", adjustflag="2"), row(adjustflag="2")])
    wrapper, client = provider(original)
    result = wrapper.fetch_daily_bars(req)
    assert result.usable and len(client.calls) == 1
    assert client.calls[0][1]["adjustment_mode"] == "forward_adjusted"
    assert all(record["adjustment_mode"] == "forward_adjusted" for record in result.records)
    assert "anchor_date" not in result.response["parameters"]


@pytest.mark.parametrize("change", [{"raw_hash": "0" * 64}, {"parameters": {}}, {"login": {"ok": False, "error_code": "0", "error_msg": "not logged in"}}])
def test_malformed_success_is_unusable_without_mutating_source(change):
    original = reply()
    original.update(change)
    wrapper, _ = provider(original)
    result = wrapper.fetch_daily_bars(request())
    assert result.status == "schema_changed" and not result.usable and not result.records
    assert result.response is original and original["ok"] is True


def test_mode_isolation_rejects_fake_research_and_cross_mode_response(monkeypatch):
    with pytest.raises(ValueError, match="offline_test"):
        BaoStockProvider(FakeClient(reply()))
    with pytest.raises(ValueError):
        BaoStockProvider(FakeClient(reply()), mode="demo")
    client = BaoStockF2Client()
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: reply())
    result = BaoStockProvider(client).fetch_daily_bars(request())
    assert result.status == "schema_changed" and not result.usable
    live_marked = reply()
    live_marked["provenance_mode"] = "online"
    wrapper, _ = provider(live_marked)
    assert wrapper.fetch_daily_bars(request()).status == "schema_changed"
    invalid_mode = reply()
    invalid_mode["verification_kind"] = ["offline_test"]
    wrapper, _ = provider(invalid_mode)
    assert wrapper.fetch_daily_bars(request()).status == "schema_changed"


@pytest.mark.parametrize("stamp", ["2099-01-01T21:00:00+08:00", "2026-09-09T21:00:00+08:00"])
def test_future_observation_or_future_market_request_cannot_be_usable(stamp):
    original = reply()
    original["fetched_at"] = stamp
    wrapper, _ = provider(original)
    result = wrapper.fetch_daily_bars(request())
    assert result.status == "schema_changed" and not result.usable
    assert result.response is original and result.quality["validation_error"] == "source_market_date_in_future"


def test_unsupported_adjustment_quote_and_universe_do_not_call_sdk():
    wrapper, client = provider(reply())
    result = wrapper.fetch_daily_bars(request(mode="backward_adjusted"))
    quote = wrapper.fetch_quote(IDENTITY, target_date=DAY)
    assert result.status == "unsupported" and result.metrics["requests"] == 0
    assert result.response["error_code"] == "unsupported_adjustment" and "login" not in result.response
    assert quote.status == "unsupported" and quote.snapshot is None
    with pytest.raises(NotImplementedError):
        wrapper.fetch_universe()
    assert client.calls == []


@pytest.mark.parametrize("rows,verified", [([{"calendar_date": DAY, "is_trading_day": "0"}], True), ([], False)])
def test_calendar_preserves_observed_closed_day_and_rejects_empty(rows, verified):
    original = reply(rows=rows, operation="calendar", parameters={"start_date": DAY, "end_date": DAY})
    wrapper, client = provider(original)
    result = wrapper.fetch_trading_calendar(start_date=DAY, end_date=DAY)
    assert result.response is original and result.quality["calendar_verified"] is verified
    assert result.status == ("success" if verified else "partial")
    assert result.quality["calendar"] == ({DAY: False} if verified else {})
    assert client.calls == [("calendar", {"start_date": DAY, "end_date": DAY})]


@pytest.mark.parametrize("owns", [False, True])
def test_close_respects_client_ownership(owns):
    client = FakeClient(reply())
    with BaoStockProvider(client, mode="offline_test", owns_client=owns):
        assert client.close_count == 0
    assert client.close_count == int(owns)


@pytest.mark.parametrize("source_stopped,expected_calls,expected_status", [(False, 4, "healthy"), (True, 1, "unavailable")])
def test_inherited_healthcheck_is_bounded_and_stops_on_permission(source_stopped, expected_calls, expected_status):
    samples = [request(SecurityIdentity("synthetic-" + board, code, exchange, board, metadata_verified=True))
               for code, exchange, board in [("600000", "SSE", "sse_main"), ("000001", "SZSE", "szse_main"),
                                             ("300750", "SZSE", "chinext"), ("688981", "SSE", "star")]]

    class HealthClient(FakeClient):
        def query(self, operation, **parameters):
            self.calls.append((operation, parameters))
            return reply(parameters=parameters, rows=[row(code=parameters["code"])],
                         error=("permission_denied", "10001006") if source_stopped else None)

    client = HealthClient(None)
    wrapper = BaoStockProvider(client, mode="offline_test")
    assert client.calls == []
    with pytest.raises(ValueError):
        wrapper.healthcheck(samples[:1])
    assert client.calls == []
    result = wrapper.healthcheck(samples)
    assert len(client.calls) == expected_calls and result["status"] == expected_status
    assert result["sample_only"] is True and result["full_market_verified"] is False
    assert result["model_calls"] == 0
