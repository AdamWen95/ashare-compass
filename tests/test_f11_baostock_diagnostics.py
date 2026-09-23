"""Offline-only stage diagnostics; synthetic samples never become live evidence."""

import importlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from ashare_daily.providers import baostock as provider
from ashare_daily.providers.baostock_worker import execute_request


class Paged:
    error_code, error_msg = "0", "success"
    fields = provider.BASIC_FIELDS
    per_page_count = "2000"

    def __init__(self, *, lose_page=False):
        self.data = [[f"sh.{i:06d}", "synthetic", "2000-01-01", "", "1", "1"] for i in range(2000)]
        self.cur_row_num, self.cur_page_num, self.lose_page = 0, "1", lose_page

    def next(self):
        if self.cur_row_num < len(self.data):
            return True
        if not self.lose_page:
            self.data, self.cur_row_num, self.cur_page_num = [], 0, "2"
        return False

    def get_row_data(self):
        row = self.data[self.cur_row_num]
        self.cur_row_num += 1
        return row


def sdk(result):
    success = lambda: SimpleNamespace(error_code="0", error_msg="success")
    return SimpleNamespace(login=success, logout=success, query_stock_basic=lambda **kwargs: result)


def test_diagnostic_stage_events_show_query_page_and_terminal_boundaries():
    events = []
    result = execute_request({"operation": "basic_all", "parameters": {}}, sdk=sdk(Paged()),
                             emit=events.append, diagnostic_stages=True)
    assert result["ok"] is True
    stages = [event for event in events if event["event"] == "stage"]
    assert any(e["stage"] == "query_wait" and e["state"] == "completed" for e in stages)
    assert any(e["stage"] == "page_wait" and e["requested_page"] == 2 for e in stages)
    assert any(e["stage"] == "terminal_validation" and e["state"] == "completed" for e in stages)
    assert [e["records"] for e in stages if e["stage"] == "page_observed"] == [2000, 0]
    assert all(e["stage_seconds"] >= 0 for e in stages if e["state"] == "completed")
    assert all(e["observed_at"].endswith("+08:00") for e in stages)


def test_lost_full_page_remains_failure_with_terminal_observation():
    events = []
    result = execute_request({"operation": "basic_all", "parameters": {}}, sdk=sdk(Paged(lose_page=True)),
                             emit=events.append, diagnostic_stages=True)
    assert result["ok"] is False and result["rows"] == []
    assert result["pagination"]["exhausted"] is False
    terminal = [e for e in events if e.get("stage") == "terminal_validation"]
    assert [e["state"] for e in terminal] == ["started"]
    assert terminal[0]["observed_records"] == 2000


@pytest.mark.parametrize("stage", ["login_wait", "query_wait", "page_wait"])
def test_hard_timeout_keeps_exact_last_started_stage(monkeypatch, stage):
    events = [{"event": "stage", "stage": stage, "state": "started", "elapsed_seconds": 0.1}]
    if stage != "login_wait":
        events.insert(0, {"event": "login", "login": {"ok": True, "error_code": "0", "error_msg": "success"}})

    def run(command, **kwargs):
        assert json.loads(kwargs["input"])["diagnostic_stages"] is True
        raise subprocess.TimeoutExpired(command, 1, output="\n".join(json.dumps(e) for e in events).encode())

    monkeypatch.setattr(provider.subprocess, "run", run)
    result = provider.BaoStockClient(timeout_seconds=1, max_attempts=1, diagnostic_stages=True).query("basic_all")
    assert result["diagnostics"]["interrupted_stage"] == stage
    assert result["diagnostics"]["failure_stage"] == stage
    assert result["diagnostics"]["hard_timeout"] is True
    assert result["rows"] == []
    assert result["attempts"][0]["diagnostics"] == result["diagnostics"]


def test_default_worker_protocol_remains_unchanged():
    events = []
    execute_request({"operation": "basic_all", "parameters": {}}, sdk=sdk(Paged()), emit=events.append)
    assert [e["event"] for e in events] == ["login", "query"]


def test_query_return_error_is_not_misclassified_as_row_read(monkeypatch):
    events = []
    failed = SimpleNamespace(error_code="10002007", error_msg="网络接收错误。", data=[], cur_page_num=1)
    result = execute_request({"operation": "basic_all", "parameters": {}}, sdk=sdk(failed),
                             emit=events.append, diagnostic_stages=True)
    assert result["ok"] is False and result["error_code"] == "10002007"
    assert not any(e.get("stage") == "row_read" for e in events)
    query_end = [e for e in events if e.get("stage") == "query_wait" and e["state"] == "completed"]
    assert query_end[0]["error_code"] == "10002007"
    stdout = "\n".join(json.dumps(e) for e in [*events, {"event": "final", "result": result}])
    monkeypatch.setattr(provider.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, stdout, ""))
    actual = provider.BaoStockClient(max_attempts=1, diagnostic_stages=True).query("basic_all")
    assert actual["diagnostics"]["failure_stage"] == "query_wait"
    assert actual["diagnostics"]["hard_timeout"] is False


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("probe_f11_baostock")


def response(operation, parameters, rows=None):
    value = provider.base_result(operation, parameters)
    value["login"] = {"ok": True, "error_code": "0", "error_msg": "success"}
    if rows is not None:
        value.update(ok=True, status="ok" if rows else "empty_confirmed", rows=rows, error_code="0", error_msg="success")
    return value


class ProbeClient:
    def __init__(self, latest="2026-09-11", *, calendar_missing=False):
        self.calls, self.latest, self.calendar_missing = [], latest, calendar_missing

    def query(self, operation, **parameters):
        self.calls.append((operation, parameters))
        if operation == "calendar":
            # Explicit synthetic calendar fixture, no weekday fallback in code.
            rows = [{"calendar_date": f"2026-09-{day:02d}", "is_trading_day": "1" if day in [4, 7, 8, 9, 10, 11] else "0"}
                    for day in range(4, 12 if not self.calendar_missing else 11)]
            return response(operation, parameters, rows)
        if operation == "history":
            return response(operation, parameters, [{"date": self.latest, "code": "sh.600000"}])
        value = response(operation, parameters)
        value.update(status="timeout", error_code="worker_timeout", diagnostics={"failure_stage": "query_wait"})
        return value


def sample_response(operation, code, start, end):
    value = response(operation, {"code": code, "start_date": start, "end_date": end})
    value.update(status="schema_changed", error_code="10004011")
    return value


def test_probe_rejects_test_client_with_live_environment_label(probe, tmp_path):
    with pytest.raises(ValueError, match="offline_test"):
        probe.run_probe(probe.date(2026, 9, 11), tmp_path / "forbidden", "workstation-network-enabled", client=ProbeClient())
    assert not (tmp_path / "forbidden").exists()


def test_stale_sample_drives_explicit_diagnostic_date_without_claiming_target(probe, tmp_path):
    client = ProbeClient("2026-09-10")
    manifest = probe.run_probe(probe.date(2026, 9, 11), tmp_path / "evidence", "offline_test",
                               client=client, sdk_sample=sample_response)
    assert manifest["verification_kind"] == "offline_test"
    assert manifest["target_is_trading_day"] is True
    assert manifest["sample_target_date_present"] is False
    assert manifest["discovery_date"] == "2026-09-10"
    assert manifest["discovery_is_target_date"] is False
    assert manifest["requested_date"] == "2026-09-11"
    assert [op for op, _ in client.calls] == ["calendar", "history", "universe", "basic_all"]
    assert client.calls[2][1] == {"day": "2026-09-10"}
    assert manifest["request_count"] == 7
    assert manifest["universe_verified"] is False
    assert all(value is None for value in manifest["board_market_totals"].values())
    assert manifest["records"][2]["failure_category"] == "query_wait_failed"
    assert manifest["records"][-1]["failure_category"] == "source_rejected_capability_parameters"
    assert all((tmp_path / "evidence" / record["response_path"]).is_file() for record in manifest["records"])


def test_incomplete_calendar_stops_before_quote_or_discovery(probe, tmp_path):
    client = ProbeClient(calendar_missing=True)
    manifest = probe.run_probe(probe.date(2026, 9, 11), tmp_path / "evidence", "offline_test",
                               client=client, sdk_sample=sample_response)
    assert len(client.calls) == 1
    assert manifest["target_is_trading_day"] is None
    assert manifest["source_stop_reason"].startswith("calendar_unverified")


def test_login_network_error_is_not_permission_or_purchase_requirement(probe):
    value = provider.base_result("calendar", {})
    value.update(error_code="10002007")
    assert probe.failure_category(value) == "login_failed"
    value.update(status="permission_denied", error_code="10001006")
    assert probe.failure_category(value) == "source_access_denied"


def test_v1_raw_query_error_takes_precedence_over_old_row_read_label(probe):
    value = response("basic_all", {})
    value.update(error_code="10002007", diagnostics={"failure_stage": "row_read", "events": [
        {"stage": "query_wait", "state": "completed", "error_code": "10002007"},
        {"stage": "row_read", "state": "started"},
    ]})
    assert probe.failure_category(value) == "query_response_failed"
    assert value["diagnostics"]["failure_stage"] == "row_read"  # Original evidence is immutable.
