"""Offline-only EastMoney parser, access gates and bounded HTTP contracts."""
import base64
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import queue
import time

import pytest

from ashare_daily.providers.base import DailyBarRequest, SecurityIdentity
from ashare_daily.providers.eastmoney import (
    ADJUSTMENTS, EastMoneyProvider, HISTORY_ENDPOINT, QUOTE_ENDPOINT,
    history_parameters, normalize_eastmoney_response, quote_parameters,
)
from ashare_daily.providers import http
from ashare_daily.providers import eastmoney
from test_provider_storage import em_response, kline, request as stored_request


PERMISSION = {"enabled": True, "permission_status": "approved", "permission_basis": "isolated offline test fixture only",
              "purpose": "personal_noncommercial_local_research", "permitted_storage": True,
              "permitted_automated_access": True, "llm_export": False}
STAMP = "2026-09-11T20:00:00+08:00"


@pytest.fixture(autouse=True)
def fixed_observation_clock(monkeypatch):
    monkeypatch.setattr(eastmoney, "_now", lambda: datetime.fromisoformat("2026-09-12T00:00:00+08:00"))


def fixture_http(endpoint, parameters, payload, *, status=200):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"schema_version": "bounded-http-response-v1", "request": {"url": endpoint, "parameters": parameters},
            "http_status": status, "headers": {}, "body_base64": base64.b64encode(raw).decode("ascii"),
            "body_sha256": hashlib.sha256(raw).hexdigest(), "body_complete": True, "fetched_at": STAMP,
            "provenance_mode": "offline_test", "verification_kind": "offline_test", "ok": status == 200,
            "status": "received" if status == 200 else "permission_denied" if status in {401, 403} else "rate_limited",
            "error_code": "0" if status == 200 else "http_" + str(status), "error_msg": "offline fixture",
            "metrics": {"requests": 1, "retries": 0}}


def provider(payload=None, *, status=200, permission=None):
    seen = []
    def transport(endpoint, parameters):
        seen.append((endpoint, parameters))
        return fixture_http(endpoint, parameters, payload, status=status)
    return EastMoneyProvider(permission=PERMISSION if permission is None else permission, mode="offline_test", transport=transport), seen


def test_default_disabled_and_incomplete_permissions_never_start_network(monkeypatch):
    monkeypatch.setattr(http.HttpClient, "get", lambda *args, **kwargs: pytest.fail("no HTTP allowed"))
    req = stored_request()
    for policy in (None, {}, {**PERMISSION, "enabled": False}, {**PERMISSION, "permission_status": "unconfirmed"}):
        client = EastMoneyProvider(permission=policy)
        result = client.fetch_daily_bars(req)
        assert result.status == "permission_required" and result.metrics["requests"] == 0
        assert result.response["network_sent"] is False and result.response["http"] is None
        assert result.response["verification_kind"] == "not_requested"
        assert not result.has_facts and not result.usable
        assert client.fetch_quote(req.identity, target_date=req.end_date).status == "permission_required"
        client.close()
    assert EastMoneyProvider(permission={"enabled": False}, mode="offline_test").fetch_daily_bars(req).metrics["requests"] == 0


@pytest.mark.parametrize("field,value", [("permission_basis", ""), ("purpose", "commercial"),
                                        ("permitted_storage", False), ("permitted_automated_access", False),
                                        ("llm_export", True), ("llm_export", None), ("enabled", 1)])
def test_each_permission_boundary_is_required(field, value):
    client, seen = provider(permission={**PERMISSION, field: value})
    assert client.fetch_daily_bars(stored_request()).status == "permission_required"
    assert seen == []


def test_live_test_transport_separation_and_bounded_arguments():
    with pytest.raises(ValueError, match="injected"):
        EastMoneyProvider(permission=PERMISSION, transport=lambda *args: {})
    with pytest.raises(ValueError, match="explicit test"):
        EastMoneyProvider(permission=PERMISSION, mode="offline_test")
    for kw in ({"max_attempts": 3}, {"timeout_seconds": 0}, {"timeout_seconds": float("inf")}, {"pause_seconds": -1}):
        with pytest.raises(ValueError):
            EastMoneyProvider(**kw)


@pytest.mark.parametrize("board,exchange,code,secid", [("sse_main", "SSE", "600519", "1.600519"),
                        ("star", "SSE", "688001", "1.688001"), ("szse_main", "SZSE", "000001", "0.000001"),
                        ("chinext", "SZSE", "300750", "0.300750")])
@pytest.mark.parametrize("adjustment,fqt", ADJUSTMENTS.items())
def test_verified_exchange_identity_and_explicit_adjustment_parameters(board, exchange, code, secid, adjustment, fqt):
    identity = SecurityIdentity("fixture-" + board, code, exchange, board, metadata_verified=True)
    req = DailyBarRequest(identity, "2026-09-10", "2026-09-11", ("2026-09-10", "2026-09-11"), adjustment)
    payload = {"rc": 0, "data": {"code": code, "klines": [kline(day) for day in req.expected_dates]}}
    client, seen = provider(payload)
    result = client.fetch_daily_bars(req)
    assert seen[0][0] == HISTORY_ENDPOINT
    assert seen[0][1]["secid"] == secid and seen[0][1]["fqt"] == fqt and seen[0][1]["klt"] == "101"
    assert seen[0][1]["beg"] == "20260910" and seen[0][1]["end"] == "20260911"
    assert not {"offset", "limit", "total", "lmt"}.intersection(seen[0][1])
    assert result.status == "partial" and result.has_facts and not result.usable
    assert result.quality["calendar_coverage_complete"] is True
    assert result.source_business_date is None and result.response["network_sent"] is False
    assert all(row["adjustment_mode"] == adjustment and row["provider"] == "eastmoney" for row in result.records)


def test_history_normalizes_lots_and_yuan_without_inventing_preclose_or_status():
    req = stored_request()
    response = em_response(req, lines=[kline(volume="123.45", amount="123450.50")])
    quality = normalize_eastmoney_response(response, req, mode="offline_test")
    row = quality["records"][0]
    assert row["volume_shares"] == 12345 and row["amount_cny"] == "123450.5"
    assert row["preclose"] is row["tradestatus"] is row["is_st"] is None
    assert row["turnover_ratio"] == "0.02" and row["provider_change_ratio"] == "0"
    assert "missing_preclose" in row["quality_flags"]
    assert not quality["quote_complete"] and not quality["status_complete"] and not quality["complete"]
    assert quality["valid_quote_dates"] == [] and quality["suspended_dates"] == []
    assert quality["source_row_limit"] is None


@pytest.mark.parametrize("lines,reason", [([kline(), kline()], "duplicate_date"),
                                         (["garbled"], "history_requires_eleven_source_fields"),
                                         ([kline(close="99")], "ohlc_range_invalid"),
                                         ([kline(volume="0.001")], "volume_not_integral_shares"),
                                         ([kline(volume="-1")], "invalid_number:volume"),
                                         ([kline(close="NaN")], "invalid_number:close"),
                                         ([kline(day="2026-09-09")], "date_not_in_verified_requested_calendar")])
def test_bad_rows_quarantined_with_specific_quality_reason(lines, reason):
    req = stored_request()
    quality = normalize_eastmoney_response(em_response(req, lines=lines), req, mode="offline_test")
    assert not quality["records"] and quality["missing_dates"] == list(req.expected_dates)
    assert reason in {issue["reason"] for issue in quality["quality_issues"]}


def test_empty_short_and_out_of_order_responses_do_not_prove_window_complete():
    req = stored_request(dates=("2026-09-10", "2026-09-11"))
    empty = normalize_eastmoney_response(em_response(req, lines=[], stamp=STAMP), req, mode="offline_test")
    assert empty["missing_dates"] == list(req.expected_dates) and empty["raw_row_count"] == 0
    short = normalize_eastmoney_response(em_response(req, lines=[kline(day=req.expected_dates[0])], stamp=STAMP), req, mode="offline_test")
    assert short["missing_dates"] == [req.expected_dates[1]] and not short["calendar_coverage_complete"]
    reversed_rows = [kline(day=day) for day in reversed(req.expected_dates)]
    unordered = normalize_eastmoney_response(em_response(req, lines=reversed_rows, stamp=STAMP), req, mode="offline_test")
    assert unordered["missing_dates"] == [] and unordered["quality_issues"][0]["reason"] == "source_dates_not_ordered"
    assert not unordered["calendar_coverage_complete"]


@pytest.mark.parametrize("mutation", [lambda r: r.update(provider="baostock"),
                        lambda r: r.update(source_symbol="0.688001"), lambda r: r.update(raw_hash="0" * 64),
                        lambda r: r.update(source_business_date="2026-09-11"),
                        lambda r: r["payload"]["data"].update(code="000001"),
                        lambda r: r["http"]["request"]["parameters"].update(fqt="1"),
                        lambda r: r["http"].update(body_complete=False),
                        lambda r: r["http"].update(verification_kind="live_network"),
                        lambda r: r.update(fetched_at="2026-09-11T20:00:00"),
                        lambda r: r["identity"].update(metadata_verified=False)])
def test_archive_revalidation_rejects_identity_hash_payload_mode_or_parameter_forgery(mutation):
    req = stored_request()
    response = em_response(req)
    mutation(response)
    with pytest.raises(ValueError):
        normalize_eastmoney_response(response, req, mode="offline_test")


def test_source_rc_empty_json_and_duplicate_keys_are_visible_schema_errors():
    req = stored_request()
    for payload in ({"rc": 100, "data": None}, {"rc": 0, "data": None}, {"rc": 0, "data": {"code": "000001", "klines": []}}):
        client, _ = provider(payload)
        result = client.fetch_daily_bars(req)
        assert result.status == "schema_changed" and result.response["raw_hash"]
        assert not result.has_facts and result.response["http"]
    def duplicate_transport(endpoint, parameters):
        response = fixture_http(endpoint, parameters, {})
        raw = b'{"rc":0,"rc":1,"data":null}'
        response.update(body_base64=base64.b64encode(raw).decode(), body_sha256=hashlib.sha256(raw).hexdigest())
        return response
    client = EastMoneyProvider(permission=PERMISSION, mode="offline_test", transport=duplicate_transport)
    assert client.fetch_daily_bars(req).status == "schema_changed"


@pytest.mark.parametrize("status", [401, 403, 429])
def test_permission_or_rate_refusal_latches_across_bar_and_quote_operations(status):
    client, seen = provider({"error": "offline refusal"}, status=status)
    req = stored_request()
    first = client.fetch_daily_bars(req)
    assert first.status == ("permission_denied" if status in {401, 403} else "rate_limited")
    assert first.metrics["requests"] == 1
    second = client.fetch_quote(req.identity, target_date=req.end_date)
    assert second.status == "circuit_open" and second.metrics["requests"] == 0
    assert len(seen) == 1


def test_quote_decimal_scale_and_unknown_timestamp_never_masquerade_as_daily_bar():
    req = stored_request()
    payload = {"rc": 0, "data": {"f57": req.identity.code, "f58": "离线样本", "f43": 10.25, "f44": 11,
                                "f45": 9, "f46": 10, "f60": 9.5, "f47": 123, "f48": 123450,
                                "f59": 2, "f86": 1789120800}}
    client, seen = provider(payload)
    result = client.fetch_quote(req.identity, target_date=req.end_date)
    assert seen[0] == (QUOTE_ENDPOINT, quote_parameters(req.identity))
    assert result.status == "partial" and result.snapshot.price == "10.25"
    assert result.snapshot.prev_close == "9.5" and result.snapshot.volume_shares == 12300
    assert result.snapshot.source_timestamp is None and result.snapshot.amount_cny is None
    assert "source_timestamp_unverified" in result.snapshot.quality_flags
    assert not result.snapshot.is_closing_daily_bar
    assert result.response["source_business_date"] is None


def test_healthcheck_uses_supplied_verified_four_board_identities_and_is_only_degraded():
    requests = [DailyBarRequest(SecurityIdentity("fixture-" + board, code, exchange, board, metadata_verified=True),
                               "2026-09-11", "2026-09-11", ("2026-09-11",))
                for board, code, exchange in [("sse_main", "600519", "SSE"), ("star", "688001", "SSE"),
                                              ("szse_main", "000001", "SZSE"), ("chinext", "300750", "SZSE")]]
    def transport(endpoint, parameters):
        return fixture_http(endpoint, parameters, {"rc": 0, "data": {"code": parameters["secid"].split(".")[1], "klines": [kline(day="2026-09-11")]}})
    client = EastMoneyProvider(permission=PERMISSION, mode="offline_test", transport=transport)
    health = client.healthcheck(requests)
    assert len(health["records"]) == 4 and health["status"] == "degraded"
    assert all(row["status"] == "partial" and not row["usable"] for row in health["records"])
    assert health["sample_only"] and not health["full_market_verified"] and health["model_calls"] == 0


def test_http_retries_are_bounded_and_retry_after_does_not_trigger_early_retry(monkeypatch):
    client, calls = http.HttpClient(max_attempts=2, pause_seconds=0), []
    def attempt(req):
        calls.append(req)
        result = http._empty(req)
        result.update(status="server_error", error_code="http_503", http_status=503)
        return result
    monkeypatch.setattr(client, "_attempt", attempt)
    result = client.get(HISTORY_ENDPOINT, {"secid": "1.688001"})
    assert len(calls) == 2 and result["metrics"]["retries"] == 1
    assert len(result["attempt_evidence"]) == 2
    assert all(item["body_sha256"] for item in result["attempt_evidence"])
    def retry_after(req):
        result = attempt(req)
        result["headers"] = {"retry-after": "30"}
        return result
    monkeypatch.setattr(client, "_attempt", retry_after)
    result = client.get(HISTORY_ENDPOINT, {"secid": "1.688001"})
    assert result["metrics"]["requests"] == 1


@pytest.mark.parametrize("status", [401, 403, 429])
def test_http_refusal_does_not_retry_or_read_body_or_follow_other_hosts(monkeypatch, status):
    client, calls = http.HttpClient(pause_seconds=0), []
    def attempt(req):
        calls.append(req)
        result = http._empty(req)
        result.update(status="permission_denied" if status in {401, 403} else "rate_limited", error_code="http_" + str(status), http_status=status)
        return result
    monkeypatch.setattr(client, "_attempt", attempt)
    assert client.get(HISTORY_ENDPOINT, {"secid": "1.688001"})["http_status"] == status
    assert client.get(QUOTE_ENDPOINT, {"secid": "1.688001"})["metrics"]["requests"] == 0
    assert len(calls) == 1
    with pytest.raises(ValueError):
        client.get("https://127.0.0.1/", {"secid": "1.688001"})


def test_http_hard_timeout_closes_stuck_worker_without_real_network(monkeypatch):
    class Input:
        def write(self, value): pass
        def flush(self): pass
        def close(self): pass
    class Process:
        stdin, stdout = Input(), Input()
        killed = False
        def poll(self): return None
        def kill(self): self.killed = True
        def wait(self, timeout): return 0
    client = http.HttpClient(timeout_seconds=.02, max_attempts=1, pause_seconds=0)
    process = Process()
    client._process, client._events = process, queue.Queue()
    started = time.monotonic()
    result = client.get(HISTORY_ENDPOINT, {"secid": "1.688001"})
    assert result["status"] == "timeout" and result["error_code"] == "hard_timeout"
    assert process.killed and client._process is None and time.monotonic() - started < 1


class FakeResponse:
    def __init__(self, body=b'{"rc":0}', status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {"Content-Length": str(len(body))}
        self.read_calls = 0
    def getheaders(self): return list(self.headers.items())
    def read1(self, maximum):
        self.read_calls += 1
        chunk, self.body = self.body[:maximum], self.body[maximum:]
        return chunk


class FakeConnection:
    def __init__(self, responses):
        self.responses, self.requests, self.sock, self.closed = list(responses), [], None, False
    def connect(self): self.sock = self
    def settimeout(self, value): assert value > 0
    def request(self, method, path, headers): self.requests.append((method, path, headers))
    def getresponse(self): return self.responses.pop(0)
    def close(self): self.closed = True


def test_worker_reuses_connection_truthful_ua_and_raw_bytes(monkeypatch):
    raw = b'{ "rc" : 0 }\n'
    connection = FakeConnection([FakeResponse(raw), FakeResponse(raw)])
    created = []
    def factory(*args, **kwargs):
        created.append(args)
        return connection
    monkeypatch.setattr(http.http.client, "HTTPSConnection", factory)
    connections, stages = {}, []
    request = {"url": HISTORY_ENDPOINT, "parameters": {"secid": "1.688001"}}
    for _ in range(2):
        result = http._request_in_worker(request, 1, connections, stages.append)
        assert result["ok"] and result["body_complete"]
        assert result["body_sha256"] == hashlib.sha256(raw).hexdigest()
        assert base64.b64decode(result["body_base64"]) == raw
    assert len(created) == 1 and len(connection.requests) == 2
    assert "Mozilla" not in connection.requests[0][2]["User-Agent"]
    assert "personal noncommercial local research" in connection.requests[0][2]["User-Agent"]
    assert "response_headers" in stages and "read_body" in stages


@pytest.mark.parametrize("status", [301, 401, 403, 429])
def test_worker_never_follows_redirect_or_reads_access_refusal_body(monkeypatch, status):
    response = FakeResponse(status=status, headers={"Location": "https://other-host.example/", "Set-Cookie": "private"})
    connection = FakeConnection([response])
    monkeypatch.setattr(http.http.client, "HTTPSConnection", lambda *args, **kwargs: connection)
    result = http._request_in_worker({"url": QUOTE_ENDPOINT, "parameters": {"secid": "1.688001"}}, 1, {}, lambda stage: None)
    assert not result["ok"] and len(connection.requests) == 1 and connection.closed
    assert "set-cookie" not in result["headers"]
    if status in {401, 403, 429}:
        assert response.read_calls == 0 and not result["body_complete"]


def test_worker_rejects_truncation_body_size_and_compression(monkeypatch):
    cases = [(FakeResponse(b"abc", headers={"Content-Length": "50"}), "body_length_mismatch"),
             (FakeResponse(b"x" * (http.MAX_BODY_BYTES + 10)), "body_too_large"),
             (FakeResponse(b"abc", headers={"Content-Encoding": "gzip"}), "unsupported_content_encoding")]
    for response, error in cases:
        connection = FakeConnection([response])
        monkeypatch.setattr(http.http.client, "HTTPSConnection", lambda *args, **kwargs: connection)
        result = http._request_in_worker({"url": HISTORY_ENDPOINT, "parameters": {"secid": "1.688001"}}, 1, {}, lambda stage: None)
        assert not result["ok"] and result["error_code"] == error
        assert len(base64.b64decode(result["body_base64"])) <= http.MAX_BODY_BYTES


def test_future_observation_and_requested_dates_are_not_accepted():
    req = stored_request()
    response = em_response(req, stamp="2026-09-13T00:00:00+08:00")
    with pytest.raises(ValueError, match="future fetched_at"):
        normalize_eastmoney_response(response, req, mode="offline_test")
    response = em_response(req, stamp="2026-09-01T00:00:00+08:00")
    with pytest.raises(ValueError, match="after observation"):
        normalize_eastmoney_response(response, req, mode="offline_test")
    payload = {"rc": 0, "data": {"f57": req.identity.code, "f58": "离线样本"}}
    client, _ = provider(payload)
    result = client.fetch_quote(req.identity, target_date="2026-09-12")
    assert result.status == "schema_changed" and result.snapshot is None
    assert "after observation" in result.response["error_msg"]


def test_offline_metrics_do_not_report_live_network():
    req = stored_request()
    client, _ = provider(em_response(req)["payload"])
    result = client.fetch_daily_bars(req)
    assert result.metrics["mode"] == "offline_test"
    assert result.metrics["verification_kind"] == "offline_test"
    assert result.metrics["network_requests"] == 0 and result.metrics["network_sent"] is False


def test_decimal_normalization_does_not_round_to_global_context_precision():
    req = stored_request()
    amount = "123456789012345678901234567890.12"
    parts = kline(amount=amount).split(",")
    parts[8] = "0.12345678901234567890123456789"
    quality = normalize_eastmoney_response(em_response(req, lines=[",".join(parts)]), req, mode="offline_test")
    assert quality["records"][0]["amount_cny"] == amount
    assert quality["records"][0]["provider_change_ratio"] == "0.0012345678901234567890123456789"
    huge = normalize_eastmoney_response(em_response(req, lines=[kline(volume="922337203685477581")]), req, mode="offline_test")
    assert huge["records"] == [] and huge["quality_issues"][0]["reason"] == "volume_exceeds_storage_integer_bound"
