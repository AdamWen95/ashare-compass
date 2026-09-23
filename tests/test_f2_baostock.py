"""F2 provider/window tests use synthetic replies and no real SDK network."""

from datetime import date, timedelta
import io
import json
import queue
from types import SimpleNamespace

import pytest

from ashare_daily.providers import baostock as base
from ashare_daily.providers.baostock_f2 import BaoStockF2Client, resolve_history_calendar
from ashare_daily.providers.baostock_worker import execute_request


PARAMETERS = {"code": "sh.600000", "start_date": "2025-01-01", "end_date": "2026-09-11", "security_type": "stock"}


def result(operation, parameters, rows=None):
    parameters = base.validate_request(operation, parameters)
    fields = base.expected_fields(operation, parameters)
    value = base.base_result(operation, parameters)
    value.update(ok=True, status="ok" if rows else "empty_confirmed", error_code="0", fields=fields, rows=rows or [],
                 raw_hash=base.raw_hash(fields, rows or []), login={"ok": True, "error_code": "0", "error_msg": "success"})
    return value


def test_history_f2_has_separate_long_window_contract_without_relaxing_old_history():
    assert base.validate_request("history_f2", PARAMETERS)["adjustment_mode"] == "unadjusted"
    with pytest.raises(ValueError, match="366"):
        base.validate_request("history", PARAMETERS)
    with pytest.raises(ValueError, match="731"):
        base.validate_request("history_f2", {**PARAMETERS, "start_date": "2024-01-01"})
    for extra in ({"as_of": "2026-09-11"}, {"anchor_date": "2026-09-11"}, {"offset": "1"}):
        with pytest.raises(ValueError):
            base.validate_request("history_f2", {**PARAMETERS, **extra})


def test_history_f2_still_rejects_bse_and_non_string_or_overlong_rows():
    with pytest.raises(ValueError):
        base.validate_request("history_f2", {**PARAMETERS, "code": "bj.920163"})
    row = dict(zip(base.HISTORY_STOCK_FIELDS, ["2026-09-11", "sh.600000", "1", "1", "1", "1", "1", "100", "100", "3", "1", "0"]))
    reply = result("history_f2", PARAMETERS, [row] * 501)
    assert base.BaoStockClient._valid_worker_result(reply, "history_f2", reply["parameters"]) is False


class SDKResult:
    error_code, error_msg = "0", "success"
    fields = base.HISTORY_STOCK_FIELDS

    def next(self):
        return False


class SDK:
    def __init__(self):
        self.logins, self.logouts, self.history = 0, 0, []

    def login(self):
        self.logins += 1
        return SDKResult()

    def logout(self):
        self.logouts += 1
        return SDKResult()

    def query_history_k_data_plus(self, code, fields, **kwargs):
        self.history.append((code, fields, kwargs))
        return SDKResult()


def test_serial_worker_reuses_real_login_and_never_invents_adjustment_anchor():
    sdk, state = SDK(), {}
    first = execute_request({"operation": "history_f2", "parameters": PARAMETERS}, sdk=sdk, session_state=state)
    second = execute_request({"operation": "history_f2", "parameters": {**PARAMETERS, "adjustment_mode": "forward_adjusted"}}, sdk=sdk, session_state=state)
    assert first["ok"] and second["ok"]
    assert sdk.logins == 1 and sdk.logouts == 0
    assert first["session"]["login_reused"] is False and second["session"]["login_reused"] is True
    assert first["session"]["login_at"] == second["session"]["login_at"]
    assert sdk.history[1][2] == {"start_date": PARAMETERS["start_date"], "end_date": PARAMETERS["end_date"], "frequency": "d", "adjustflag": "2"}


def attach_fake_worker(monkeypatch, client, behavior):
    starts = []

    def start():
        client._events, client._stderr, client._session_requests = queue.Queue(), [], 0
        process = SimpleNamespace(killed=False, returncode=None)
        process.poll = lambda: process.returncode
        process.kill = lambda: setattr(process, "returncode", -9)
        process.wait = lambda **kwargs: process.returncode

        class Input(io.StringIO):
            def flush(self):
                request = json.loads(self.getvalue().splitlines()[-1])
                behavior(client, request)

        process.stdin, process.stdout, process.stderr = Input(), io.StringIO(), io.StringIO()
        client._process = process
        starts.append(process)

    monkeypatch.setattr(client, "_start", start)
    return starts


def deliver(client, request, reply):
    client._events.put(json.dumps({"event": "final", "request_id": request["request_id"], "result": reply}) + "\n")


def test_session_parent_reuses_process_and_validates_each_response(monkeypatch):
    client = BaoStockF2Client(max_attempts=1)
    starts = attach_fake_worker(monkeypatch, client, lambda c, r: deliver(c, r, result(r["operation"], r["parameters"])))
    assert client.query("history_f2", **PARAMETERS)["ok"]
    assert client.query("history_f2", **PARAMETERS)["ok"]
    assert len(starts) == 1
    client.close()


def test_session_timeout_kills_worker_and_finite_retry_reconnects(monkeypatch):
    client = BaoStockF2Client(timeout_seconds=0.02, max_attempts=2, pause_seconds=0)
    calls = []

    def behavior(c, request):
        calls.append(request)
        if len(calls) == 2:
            deliver(c, request, result(request["operation"], request["parameters"]))

    starts = attach_fake_worker(monkeypatch, client, behavior)
    reply = client.query("history_f2", **PARAMETERS)
    assert reply["ok"] and len(starts) == 2
    assert starts[0].returncode == -9
    assert [a["error_code"] for a in reply["attempts"]] == ["worker_timeout", "0"]
    client.close()


@pytest.mark.parametrize("code,status", [("10001006", "permission_denied"), ("10001005", "rate_limited")])
def test_permission_or_limit_stops_source_without_later_network(monkeypatch, code, status):
    client = BaoStockF2Client(max_attempts=2, pause_seconds=0)

    def behavior(c, request):
        reply = base.base_result(request["operation"], request["parameters"])
        reply.update(error_code=code, status=status)
        deliver(c, request, reply)

    starts = attach_fake_worker(monkeypatch, client, behavior)
    first = client.query("history_f2", **PARAMETERS)
    second = client.query("history_f2", **PARAMETERS)
    assert first["status"] == second["status"] == status
    assert len(first["attempts"]) == 1 and second["source_stopped"]
    assert len(starts) == 1


@pytest.mark.parametrize("mutation", [lambda r: r.update(raw_hash="0" * 64), lambda r: r.update(parameters={}), lambda r: r.update(fetched_at="2026-09-11T00:00:00")])
def test_session_corrupt_protocol_never_returns_success(monkeypatch, mutation):
    client = BaoStockF2Client(max_attempts=2)

    def behavior(c, request):
        reply = result(request["operation"], request["parameters"])
        mutation(reply)
        deliver(c, request, reply)

    starts = attach_fake_worker(monkeypatch, client, behavior)
    reply = client.query("history_f2", **PARAMETERS)
    assert not reply["ok"] and reply["error_code"] == "worker_protocol_error"
    assert len(starts) == 1


def test_session_concurrent_query_is_rejected_before_network():
    client = BaoStockF2Client()
    client._busy.acquire()
    with pytest.raises(RuntimeError, match="禁止并发"):
        client.query("history_f2", **PARAMETERS)
    client._busy.release()
    with pytest.raises(ValueError):
        client.query("universe", day="2026-09-11")


class CalendarClient:
    def __init__(self, missing=False, failed=False):
        self.calls, self.missing, self.failed = [], missing, failed

    def query(self, operation, **parameters):
        self.calls.append(parameters)
        if self.failed:
            return base.base_result(operation, parameters)
        start, end = (date.fromisoformat(parameters[k]) for k in ("start_date", "end_date"))
        # Deliberately synthetic fixture statuses, never a production weekday rule.
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        rows = [{"calendar_date": d.isoformat(), "is_trading_day": "1" if d.toordinal() % 3 else "0"} for d in days]
        if end == date(2026, 9, 11):
            rows[-1]["is_trading_day"] = "1"
        if self.missing:
            rows.pop(12)
        return result(operation, parameters, rows)


def test_long_calendar_uses_two_verified_chunks_and_reuses_f1_cache(tmp_path):
    source = CalendarClient()
    actual = resolve_history_calendar(date(2026, 9, 11), tmp_path, client=source, mode="offline_test")
    assert actual["verified"] and actual["calendar_verified"]
    assert len(actual["trading_dates"]) == 320
    assert len(source.calls) == 2
    assert all((date.fromisoformat(p["end_date"]) - date.fromisoformat(p["start_date"])).days == 365 for p in source.calls)
    assert len(actual["calendar"]) == 732
    cached_source = CalendarClient(failed=True)
    cached = resolve_history_calendar(date(2026, 9, 11), tmp_path, client=cached_source, mode="offline_test")
    assert cached["verified"] and cached["trading_dates"] == actual["trading_dates"]
    assert cached_source.calls == []
    assert all(PathItem["path"] for PathItem in cached["source_metadata"])


@pytest.mark.parametrize("kwargs", [{"missing": True}, {"failed": True}])
def test_calendar_failure_cannot_guess_320_dates_or_use_partial_list(tmp_path, kwargs):
    source = CalendarClient(**kwargs)
    actual = resolve_history_calendar(date(2026, 9, 11), tmp_path, client=source, mode="offline_test")
    assert not actual["verified"] and actual["trading_dates"] == []
    assert len(source.calls) == 1


def test_calendar_real_test_separation_and_same_time_conflict(tmp_path):
    with pytest.raises(ValueError):
        resolve_history_calendar(date(2026, 9, 11), tmp_path, client=CalendarClient())
    with pytest.raises(ValueError):
        resolve_history_calendar(date(2026, 9, 11), tmp_path / "research", client=CalendarClient(), mode="offline_test")
    first = resolve_history_calendar(date(2026, 9, 11), tmp_path, client=CalendarClient(), mode="offline_test")
    path = tmp_path / "same-time-conflict.json"
    packet = json.loads(open(first["source_metadata"][0]["path"], encoding="utf-8").read())
    packet["response"]["rows"][-1]["is_trading_day"] = "0"
    packet["response"]["raw_hash"] = base.raw_hash(packet["response"]["fields"], packet["response"]["rows"])
    from ashare_daily.calendar import _digest
    packet.pop("content_hash")
    packet["content_hash"] = _digest(packet)
    path.write_text(json.dumps(packet), encoding="utf-8")
    actual = resolve_history_calendar(date(2026, 9, 11), tmp_path, client=CalendarClient(), mode="offline_test")
    assert not actual["verified"] and "冲突" in actual["validation_error"]


def test_probe_preserves_suspension_null_flags_separate_from_window_completeness(monkeypatch):
    import importlib
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    check_window = importlib.import_module("probe_f2_baostock").check_window
    row = dict(zip(base.HISTORY_STOCK_FIELDS, ["2026-09-11", "sh.600000", "1", "1", "1", "1", "1", "", "", "3", "0", "0"]))
    reply = result("history_f2", PARAMETERS, [row])
    checked = check_window(reply, "sh.600000", ["2026-09-11"], "unadjusted")
    assert checked["window_response_complete"] is True
    assert checked["verified"] is False
    assert {item["flag"] for item in checked["quality_flags"]} == {"missing_volume_shares", "missing_amount_cny"}
    assert checked["status_counts"]["normal_trade"] == 0
    assert checked["status_counts"]["suspended"] == 1
    assert reply["rows"][0]["volume"] == ""  # Never turn missing quantity into zero.
