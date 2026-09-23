"""BaoStock 适配器离线测试；SDK 和子进程均使用假对象，绝不连接服务。"""

from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime
from types import SimpleNamespace

import pytest

from ashare_daily.providers import baostock as provider
from ashare_daily.providers import baostock_worker as worker
from ashare_daily.providers.baostock_worker import BoundedSDKLog, execute_request


DATES = {"start_date": "2026-09-08", "end_date": "2026-09-08"}


class FakeResult:
    def __init__(self, fields=None, rows=None, code="0", message="success", fail_iteration=False, next_value=False):
        self.fields = fields or []
        self.rows = rows or []
        self.error_code = code
        self.error_msg = message
        self.index = 0
        self.fail_iteration = fail_iteration
        self.next_value = next_value

    def next(self):
        if self.index < len(self.rows):
            return True
        if self.fail_iteration:
            self.error_code, self.error_msg = "10002007", "网络接收错误"
        return self.next_value

    def get_row_data(self):
        value = self.rows[self.index]
        self.index += 1
        return value


class FakeSDK:
    def __init__(self, response=None, login=None):
        self.response = response
        self.login_response = login or FakeResult()
        self.calls = []

    def login(self):
        self.calls.append(("login", {}))
        return self.login_response

    def logout(self):
        self.calls.append(("logout", {}))
        return FakeResult()

    def query_trade_dates(self, **parameters):
        self.calls.append(("calendar", parameters))
        return self.response

    def query_stock_basic(self, **parameters):
        self.calls.append(("basic", parameters))
        return self.response

    def query_history_k_data_plus(self, code, fields, **parameters):
        self.calls.append(("history", {"code": code, "fields": fields, **parameters}))
        return self.response


def calendar_result():
    sdk = FakeSDK(FakeResult(provider.CALENDAR_FIELDS, [["2026-09-08", "1"]]))
    return execute_request({"operation": "calendar", "parameters": DATES}, sdk=sdk)


def completed(result):
    return subprocess.CompletedProcess([], 0, json.dumps({"event": "final", "result": result}) + "\n", "login success!\nlogout success!\n")


def test_worker_query_calendar_preserves_raw_strings_and_progress():
    events = []
    sdk = FakeSDK(FakeResult(provider.CALENDAR_FIELDS, [["2026-09-08", "1"]]))
    result = execute_request({"operation": "calendar", "parameters": DATES}, sdk=sdk, emit=events.append)
    assert result["ok"] is True
    assert result["rows"] == [{"calendar_date": "2026-09-08", "is_trading_day": "1"}]
    assert result["raw_hash"] == provider.raw_hash(result["fields"], result["rows"])
    assert datetime.fromisoformat(result["fetched_at"]).utcoffset().total_seconds() == 28800
    assert [name for name, _ in sdk.calls] == ["login", "calendar", "logout"]
    assert [event["event"] for event in events] == ["login", "query"]


@pytest.mark.parametrize("security_type,fields", [("stock", provider.HISTORY_STOCK_FIELDS), ("index", provider.HISTORY_INDEX_FIELDS)])
def test_history_requests_provider_specific_fields_without_synthetic_columns(security_type, fields):
    row = ["2026-09-08", "sh.000001", "1.00", "2.00", "1.00", "2.00", "1.00", "100", "200"]
    if security_type == "stock":
        row += ["3", "1", "0"]
    sdk = FakeSDK(FakeResult(fields, [row]))
    result = execute_request({"operation": "history", "parameters": {"code": "sh.000001", **DATES, "security_type": security_type}}, sdk=sdk)
    assert result["ok"] is True
    assert sdk.calls[1][1] == {"code": "sh.000001", "fields": ",".join(fields), **DATES, "frequency": "d", "adjustflag": "3"}
    assert result["rows"][0]["close"] == "2.00"
    assert result["parameters"]["adjustment_mode"] == "unadjusted"
    if security_type == "index":
        assert "adjustflag" not in result["rows"][0]
        assert "tradestatus" not in result["fields"]


def test_basic_retains_security_identity_fields_and_empty_out_date():
    row = ["sh.600000", "示例证券名称", "1999-11-10", "", "1", "1"]
    sdk = FakeSDK(FakeResult(provider.BASIC_FIELDS, [row]))
    result = execute_request({"operation": "basic", "parameters": {"code": "sh.600000"}}, sdk=sdk)
    assert result["rows"][0]["outDate"] == ""
    assert result["rows"][0]["type"] == "1"


def test_worker_empty_is_only_confirmed_after_successful_query():
    sdk = FakeSDK(FakeResult(provider.CALENDAR_FIELDS, []))
    result = execute_request({"operation": "calendar", "parameters": DATES}, sdk=sdk)
    assert result["ok"] is True
    assert result["status"] == "empty_confirmed"


@pytest.mark.parametrize("code,status", [("10001006", "permission_denied"), ("10001005", "rate_limited"), ("10001004", "schema_changed"), ("10002008", "timeout"), ("10002007", "unknown")])
def test_login_failure_stops_query_and_preserves_sdk_error(code, status):
    sdk = FakeSDK(login=FakeResult(code=code, message="测试错误"))
    result = execute_request({"operation": "calendar", "parameters": DATES}, sdk=sdk)
    assert result["ok"] is False
    assert result["status"] == status
    assert result["error_code"] == code
    assert result["login"]["ok"] is False
    assert sdk.calls == [("login", {})]
    assert result["rows"] == []


@pytest.mark.parametrize("response", [
    None,
    FakeResult(["calendar_date"], [["2026-09-08"]]),
    FakeResult(["calendar_date", "calendar_date"], [["2026-09-08", "1"]]),
    FakeResult(provider.CALENDAR_FIELDS, [["2026-09-08"]]),
    FakeResult(provider.CALENDAR_FIELDS, [["2026-09-08", 1]]),
    FakeResult(provider.CALENDAR_FIELDS, [["2026-09-08", None]]),
    FakeResult(provider.CALENDAR_FIELDS, [], next_value=None),
    FakeResult(provider.CALENDAR_FIELDS, [["2026-09-08", "1"]] * 501),
])
def test_schema_failure_never_becomes_success_or_partial_rows(response):
    result = execute_request({"operation": "calendar", "parameters": DATES}, sdk=FakeSDK(response))
    assert result["ok"] is False
    assert result["status"] == "schema_changed"
    assert result["rows"] == []


def test_iteration_failure_after_returning_a_row_is_not_swallowed():
    response = FakeResult(provider.CALENDAR_FIELDS, [["2026-09-08", "1"]], fail_iteration=True)
    result = execute_request({"operation": "calendar", "parameters": DATES}, sdk=FakeSDK(response))
    assert result["ok"] is False
    assert result["error_code"] == "10002007"
    assert result["rows"] == []


def test_client_uses_current_python_hard_timeout_and_hidden_windows(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return completed(calendar_result())

    monkeypatch.setattr(provider.subprocess, "run", run)
    result = provider.BaoStockClient(timeout_seconds=7, max_attempts=1).query("calendar", **DATES)
    command, kwargs = calls[0]
    assert command == [provider.sys.executable, "-X", "utf8", "-m", "ashare_daily.providers.baostock_worker"]
    assert kwargs["timeout"] == 7
    assert kwargs["check"] is False
    assert "shell" not in kwargs
    if provider.os.name == "nt":
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
    else:
        assert "creationflags" not in kwargs
    assert result["ok"] is True
    assert len(result["attempts"]) == 1


def test_timeout_retains_prior_login_result_and_retries_finitely(monkeypatch):
    progress = json.dumps({"event": "login", "login": {"ok": True, "error_code": "0", "error_msg": "success"}}).encode()
    calls, pauses = [], []

    def run(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 1, output=progress, stderr=b"login success!\n")

    monkeypatch.setattr(provider.subprocess, "run", run)
    monkeypatch.setattr(provider.time, "sleep", pauses.append)
    result = provider.BaoStockClient(timeout_seconds=1, max_attempts=2).query("calendar", **DATES)
    assert result["status"] == "timeout"
    assert result["error_code"] == "worker_timeout"
    assert result["login"]["ok"] is True
    assert result["rows"] == []
    assert len(result["attempts"]) == len(calls) == 2
    assert pauses == [0.5]


@pytest.mark.parametrize("code,status", [("10001006", "permission_denied"), ("10001005", "rate_limited"), ("schema_changed", "schema_changed"), ("unrecognized", "unknown")])
def test_client_does_not_retry_permission_rate_schema_or_unknown(monkeypatch, code, status):
    failed = provider.base_result("calendar", DATES)
    failed.update(error_code=code, status=status)
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return completed(failed)

    monkeypatch.setattr(provider.subprocess, "run", run)
    monkeypatch.setattr(provider.time, "sleep", lambda _: pytest.fail("must not retry"))
    result = provider.BaoStockClient(max_attempts=3).query("calendar", **DATES)
    assert result["status"] == status
    assert len(calls) == 1


def test_client_retries_network_then_preserves_success_and_attempt_errors(monkeypatch):
    failed = provider.base_result("calendar", DATES)
    failed.update(error_code="10002007", error_msg="网络接收错误")
    results = iter([completed(failed), completed(calendar_result())])
    monkeypatch.setattr(provider.subprocess, "run", lambda *args, **kwargs: next(results))
    monkeypatch.setattr(provider.time, "sleep", lambda _: None)
    result = provider.BaoStockClient().query("calendar", **DATES)
    assert result["ok"] is True
    assert [attempt["error_code"] for attempt in result["attempts"]] == ["10002007", "0"]


@pytest.mark.parametrize("stdout", ["not-json", "", '{"event":"final","result":{}}', '{"event":"other"}'])
def test_client_rejects_missing_or_invalid_worker_protocol(monkeypatch, stdout):
    monkeypatch.setattr(provider.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, stdout, ""))
    result = provider.BaoStockClient().query("calendar", **DATES)
    assert result["status"] == "schema_changed"
    assert result["error_code"] == "worker_protocol_error"
    assert len(result["attempts"]) == 1


@pytest.mark.parametrize("mutation", [
    lambda result: result.update(raw_hash="0" * 64),
    lambda result: result.update(fetched_at="2026-09-08T20:00:00"),
    lambda result: result.update(parameters={"start_date": "2000-01-01"}),
    lambda result: result["rows"][0].update(is_trading_day=1),
])
def test_client_checks_final_raw_hash_timestamp_parameters_and_strings(monkeypatch, mutation):
    result = calendar_result()
    mutation(result)
    monkeypatch.setattr(provider.subprocess, "run", lambda *args, **kwargs: completed(result))
    actual = provider.BaoStockClient(max_attempts=1).query("calendar", **DATES)
    assert actual["status"] == "schema_changed"
    assert actual["rows"] == []


@pytest.mark.parametrize("operation,parameters", [
    ("orders", {}), ("calendar", {**DATES, "token": "forbidden"}),
    ("calendar", {"start_date": "20260908", "end_date": "2026-09-08"}),
    ("calendar", {"start_date": "2025-01-01", "end_date": "2026-09-08"}),
    ("calendar", {"start_date": "2026-09-09", "end_date": "2026-09-08"}),
    ("calendar", {"start_date": "2026-02-30", "end_date": "2026-09-08"}),
    ("basic", {"code": "600000"}), ("basic", {"code": ""}),
    ("history", {"code": "sh.600000", **DATES, "security_type": "stock", "adjustment_mode": "forward"}),
    ("history", {"code": "sh.600000", **DATES, "security_type": "ETF"}),
])
def test_invalid_request_fails_before_any_child_or_network(monkeypatch, operation, parameters):
    monkeypatch.setattr(provider.subprocess, "run", lambda *args, **kwargs: pytest.fail("invalid request spawned worker"))
    with pytest.raises(ValueError):
        provider.BaoStockClient().query(operation, **parameters)


@pytest.mark.parametrize("options", [{"max_attempts": 0}, {"max_attempts": 4}, {"max_attempts": True}, {"timeout_seconds": 0}, {"timeout_seconds": float("inf")}, {"pause_seconds": -1}, {"pause_seconds": float("nan")}])
def test_client_options_are_bounded(options):
    with pytest.raises(ValueError):
        provider.BaoStockClient(**options)


def test_sdk_log_is_bounded_and_redacts_credentials_across_writes():
    stream = io.StringIO()
    log = BoundedSDKLog(stream)
    log.write("password=")
    log.write("secret api_key=secret user_id=anonymous\n")
    log.write("x" * 10000)
    log.flush()
    assert len(stream.getvalue()) <= provider.MAX_LOG_CHARS
    assert "secret" not in stream.getvalue()
    assert "anonymous" not in stream.getvalue()
    assert "[REDACTED]" in stream.getvalue()


def test_provider_exception_is_observable_and_logout_still_runs():
    sdk = FakeSDK()

    def query(**parameters):
        raise ConnectionError("connection closed")

    sdk.query_trade_dates = query
    result = execute_request({"operation": "calendar", "parameters": DATES}, sdk=sdk)
    assert result["error_code"] == "network_exception"
    assert result["login"]["ok"] is True
    assert sdk.calls[-1][0] == "logout"


def test_chinese_error_and_stderr_remain_intact_with_explicit_utf8(monkeypatch):
    failed = provider.base_result("calendar", DATES)
    failed.update(error_code="10002007", error_msg="网络接收错误。")

    def run(command, **kwargs):
        assert command[1:3] == ["-X", "utf8"]
        assert kwargs["encoding"] == "utf-8"
        return subprocess.CompletedProcess(command, 0, json.dumps({"event": "final", "result": failed}, ensure_ascii=False), "服务器连接失败，请稍后再试。\n")

    monkeypatch.setattr(provider.subprocess, "run", run)
    result = provider.BaoStockClient(max_attempts=1).query("calendar", **DATES)
    assert result["error_msg"] == "网络接收错误。"
    assert result["sdk_log"] == "服务器连接失败，请稍后再试。\n"
    assert "\ufffd" not in json.dumps(result, ensure_ascii=False)


def test_worker_main_stdout_contains_only_progress_json_and_sdk_logs_use_stderr(monkeypatch):
    sdk = FakeSDK(FakeResult(provider.CALENDAR_FIELDS, [["2026-09-08", "1"]]))
    original_login = sdk.login

    def logged_login():
        print("登录成功：演示日志。")
        return original_login()

    sdk.login = logged_login
    input_stream = io.StringIO(json.dumps({"operation": "calendar", "parameters": DATES}))
    output_stream, error_stream = io.StringIO(), io.StringIO()
    monkeypatch.setitem(worker.sys.modules, "baostock", sdk)
    monkeypatch.setattr(worker.sys, "stdin", input_stream)
    monkeypatch.setattr(worker.sys, "stdout", output_stream)
    monkeypatch.setattr(worker.sys, "stderr", error_stream)
    assert worker.main() == 0
    events = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == ["login", "query", "final"]
    assert events[-1]["result"]["ok"] is True
    assert error_stream.getvalue() == "登录成功：演示日志。\n"


def test_logout_permission_denial_still_stops_the_source():
    sdk = FakeSDK(FakeResult(provider.CALENDAR_FIELDS, [["2026-09-08", "1"]]))
    sdk.logout = lambda: FakeResult(code="10001006", message="权限不足")
    result = execute_request({"operation": "calendar", "parameters": DATES}, sdk=sdk)
    assert result["ok"] is False
    assert result["status"] == "permission_denied"
    assert result["rows"] == []
    assert result["logout"]["error_code"] == "10001006"
