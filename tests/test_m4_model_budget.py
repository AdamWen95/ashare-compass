"""OFFLINE model transports with the real persistent daily budget integration."""
from datetime import datetime
import json
import socket
from urllib.error import URLError

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

import ashare_daily.operations.daily as daily_module
from ashare_daily.operations.budget import BudgetLedger
from ashare_daily.operations.daily import LedgerGuard
from ashare_daily.research.model import ChatCompletionsModel, HTTPResponse, complete_validated
from ashare_daily.research.model_settings import ModelSettings

DAY = "2026-09-09"
MESSAGES = [{"role": "user", "content": "OFFLINE integration fixture; return the supplied JSON shape."}]


@pytest.fixture(autouse=True)
def fixed_beijing_day(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 9, 22, 0, tzinfo=daily_module.SHANGHAI)
    monkeypatch.setattr(daily_module, "datetime", FixedDateTime)


def response(content='{"ok":true}', *, status=200, headers=None, finish="stop", refusal=None):
    return HTTPResponse(status, json.dumps({"choices": [{"finish_reason": finish, "message": {
        "role": "assistant", "content": content, "refusal": refusal}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}}).encode(), headers or {})


class OfflineTransport:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, url, headers, payload, timeout):
        assert url == "https://offline-budget.invalid/v1/chat/completions"
        self.calls += 1
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def client(path, transport, *, limit=3, run_id="offline-run", retries=1, guard=None):
    settings = ModelSettings(provider="offline_test", base_url="https://offline-budget.invalid/v1",
                             api_key=SecretStr("offline-budget-fixture-never-real"), model_name="OFFLINE-test",
                             max_calls=6, max_retries=retries)
    ledger = BudgetLedger(path)
    return ChatCompletionsModel(settings, transport=transport, sleep=lambda seconds: None,
                                attempt_guard=guard or LedgerGuard(ledger, run_id, limit))


def test_fresh_model_clients_and_run_ids_share_daily_ceiling(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    for index in range(2):
        transport = OfflineTransport(response())
        result = client(path, transport, limit=2, run_id=f"new-client-{index}").complete(MESSAGES)
        assert result["status"] == "ok" and transport.calls == 1
    unused = OfflineTransport(response())
    result = client(path, unused, limit=2, run_id="restarted-client").complete(MESSAGES)
    assert result["status"] == "budget_exhausted" and unused.calls == 0
    summary = BudgetLedger(path).summary(DAY)
    assert summary["reserved_attempts"] == 2
    assert summary["provider_reported_usage"]["total_tokens"] == 50


@pytest.mark.parametrize("failed", [socket.timeout(), URLError("OFFLINE network failure"),
                                    response(status=429, headers={"retry-after": "0"}), response(status=500)])
def test_each_retry_consumes_reservation_before_real_transport_boundary(tmp_path, failed):
    path = tmp_path / "runtime.sqlite3"
    transport = OfflineTransport(failed, response())
    model = client(path, transport, limit=2)
    observed = []
    original = model.transport
    def observing(*args):
        observed.append(BudgetLedger(path).summary(DAY)["reserved_attempts"])
        return original(*args)
    model.transport = observing
    result = model.complete(MESSAGES)
    assert result["status"] == "ok" and result["call_count"] == 2 and transport.calls == 2
    assert observed == [1, 2]
    assert BudgetLedger(path).summary(DAY)["requests_with_usage"] == 1


def test_exhaustion_between_timeout_and_retry_stops_retry(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    transport = OfflineTransport(socket.timeout(), response())
    result = client(path, transport, limit=1).complete(MESSAGES)
    assert result["status"] == "budget_exhausted" and transport.calls == 1
    assert BudgetLedger(path).summary(DAY)["status_counts"] == {"timeout": 1}


class JsonResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ok: bool


def test_json_format_repair_consumes_its_own_reservation(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    transport = OfflineTransport(response("not JSON"), response())
    result = complete_validated(client(path, transport, limit=2), MESSAGES, JsonResult)
    assert result["status"] == "ok" and result["format_repairs"] == 1
    assert BudgetLedger(path).summary(DAY)["reserved_attempts"] == 2
    assert BudgetLedger(path).summary(DAY)["provider_reported_usage"]["total_tokens"] == 50


def test_budget_stops_format_repair_without_valid_output(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    transport = OfflineTransport(response("not JSON"), response())
    result = complete_validated(client(path, transport, limit=1), MESSAGES, JsonResult)
    assert result["status"] == "budget_exhausted" and result["parsed"] is None
    assert transport.calls == 1
    assert BudgetLedger(path).summary(DAY)["reserved_attempts"] == 1


def test_repair_retry_is_also_counted_and_unknown_tokens_not_zero_filled(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    transport = OfflineTransport(response("not JSON"), socket.timeout(), response())
    result = complete_validated(client(path, transport, limit=3), MESSAGES, JsonResult)
    summary = BudgetLedger(path).summary(DAY)
    assert result["status"] == "ok" and result["format_repairs"] == 1
    assert transport.calls == 3 and summary["reserved_attempts"] == 3
    assert summary["requests_with_usage"] == 2 and not summary["usage_complete"]
    assert summary["provider_reported_usage"]["total_tokens"] == 50


def test_usage_bookkeeping_failure_keeps_charge_and_stops_client(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    class FailingGuard(LedgerGuard):
        def after_attempt(self, reservation, record):
            raise OSError("OFFLINE disk failure, sensitive details must not propagate")
    transport = OfflineTransport(response(), response())
    model = client(path, transport, guard=FailingGuard(BudgetLedger(path), "failed-write", 1))
    first = model.complete(MESSAGES)
    assert first["status"] == "budget_record_failed"
    assert first["content"] is None
    assert model.complete(MESSAGES)["status"] == "stopped" and transport.calls == 1
    assert BudgetLedger(path).summary(DAY)["status_counts"] == {"reserved": 1}
    restarted = OfflineTransport(response())
    assert client(path, restarted, limit=1).complete(MESSAGES)["status"] == "budget_exhausted"
    assert restarted.calls == 0


def test_reservation_store_failure_prevents_all_transport(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    class FailedReservation:
        def before_attempt(self):
            raise OSError("OFFLINE private database detail")
        def after_attempt(self, reservation, record):
            raise AssertionError("must never run")
    transport = OfflineTransport(response())
    result = client(path, transport, guard=FailedReservation()).complete(MESSAGES)
    assert result["status"] == "budget_exhausted" and result["call_count"] == 0
    assert transport.calls == 0 and "private database detail" not in json.dumps(result)
    assert BudgetLedger(path).summary(DAY)["reserved_attempts"] == 0


@pytest.mark.parametrize("failed", [response(status=401), response(refusal="OFFLINE refusal"), response(finish="length")])
def test_nonretryable_failures_still_count(tmp_path, failed):
    path = tmp_path / "runtime.sqlite3"
    transport = OfflineTransport(failed, response())
    result = client(path, transport).complete(MESSAGES)
    assert result["status"] in {"authentication_failed", "refused", "truncated"}
    assert transport.calls == 1
    assert BudgetLedger(path).summary(DAY)["reserved_attempts"] == 1


def test_invalid_input_does_not_consume_network_budget(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    transport = OfflineTransport(response())
    result = client(path, transport).complete([{"role": "user", "content": "bad", "tools": []}])
    assert result["status"] == "invalid_request" and transport.calls == 0
    assert BudgetLedger(path).summary(DAY)["reserved_attempts"] == 0
