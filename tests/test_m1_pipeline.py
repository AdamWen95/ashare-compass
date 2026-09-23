"""Offline integration cases. Artificial responses only exist in pytest tmp_path."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ashare_daily.market import check_market, incremental_windows, run_market
from ashare_daily.m1_settings import SAMPLE_TYPES
from ashare_daily.storage.market import MarketStore

NOW = datetime(2026, 9, 9, 11, tzinfo=ZoneInfo("Asia/Shanghai"))
TARGET = date(2026, 9, 8)
START = date(2026, 9, 1)
TRADING = {date(2026, 9, 4), date(2026, 9, 7), TARGET, date(2026, 9, 9)}


class FakeClient:
    """Explicit synthetic API responses, never a production fallback."""
    def __init__(self, *, fail_calendar=False, fail_history_symbol=None, historical_status=None, stale_symbol=None):
        self.calls = []
        self.fail_calendar = fail_calendar
        self.fail_history_symbol = fail_history_symbol
        self.historical_status = historical_status
        self.stale_symbol = stale_symbol

    def query(self, operation, **parameters):
        self.calls.append((operation, parameters))
        response = dict(ok=True, status="ok", error_code="0", error_msg="offline fake success", fields=[], rows=[],
                        fetched_at=NOW.isoformat(), elapsed_seconds=0, sdk_version="0.9.3", operation=operation,
                        parameters=parameters, login={"ok": True, "error_code": "0", "error_msg": "fake"}, attempts=[{}])
        if operation == "calendar":
            if self.fail_calendar:
                return {**response, "ok": False, "status": "timeout", "error_code": "worker_timeout", "error_msg": "offline test timeout"}
            begin, end = map(date.fromisoformat, (parameters["start_date"], parameters["end_date"]))
            response["rows"] = [{"calendar_date": (begin + timedelta(days=i)).isoformat(),
                                 "is_trading_day": "1" if begin + timedelta(days=i) in TRADING else "0"}
                                for i in range((end - begin).days + 1)]
        elif operation == "basic":
            response["rows"] = [{"code": parameters["code"], "code_name": "OFFLINE TEST ONLY",
                                 "ipoDate": "2000-01-01", "outDate": "", "status": "1",
                                 "type": "1" if SAMPLE_TYPES[parameters["code"]] == "stock" else "2"}]
        else:
            begin, end = map(date.fromisoformat, (parameters["start_date"], parameters["end_date"]))
            if begin != end and parameters["code"] == self.fail_history_symbol:
                if self.historical_status:
                    return {**response, "ok": False, "status": self.historical_status,
                            "error_code": "10001006", "error_msg": "offline test denied"}
                return {**response, "status": "empty_confirmed"}
            for day in sorted(TRADING):
                if begin <= day <= end:
                    row = {"date": day.isoformat(), "code": parameters["code"], "open": "10", "high": "11",
                           "low": "9", "close": "10.5", "preclose": "10", "volume": "100", "amount": "1050"}
                    if begin == end and self.stale_symbol == parameters["code"]:
                        row["date"] = (day - timedelta(days=1)).isoformat()
                    if parameters["security_type"] == "stock":
                        row.update(adjustflag="3", tradestatus="1", isST="0")
                    response["rows"].append(row)
        return response


def run(tmp_path, operation="collect", client=None, **kwargs):
    return run_market(operation, client=client or FakeClient(), now=NOW,
                      database=tmp_path / "research.sqlite3", output_dir=tmp_path / "evidence",
                      target_date=kwargs.pop("target_date", TARGET), **kwargs)


def test_doctor_index_without_stock_fields_and_exact_target(tmp_path):
    result = run(tmp_path, "doctor")
    assert result["status"] == "ok"
    assert result["target_trade_date"] == result["actual_latest_data_date"] == TARGET.isoformat()
    assert result["success_count"] == 7
    assert result["verification_kind"] == "offline_test"
    assert result["target_data_verified"] is True
    assert not (tmp_path / "research.sqlite3").exists()
    assert Path(result["run_directory"], "sample_bars.csv").is_file()


def test_collect_and_repeat_are_idempotent_and_keep_first_seen(tmp_path):
    first = run(tmp_path, start_date=START)
    assert first["status"] == "ok"
    assert first["database_row_count"] == first["csv_record_count"] == 21
    store = MarketStore(tmp_path / "research.sqlite3")
    original = store.read_bars(list(SAMPLE_TYPES), START, TARGET)
    assert len(original) == 21
    assert original[0].close == Decimal("10.5")
    second = run(tmp_path, start_date=START)
    assert second["status"] == "ok"
    assert second["database_row_count"] == 21
    assert sum(item["inserted"] for item in second["items"]) == 0
    assert sum(item["unchanged"] for item in second["items"]) == 21
    assert store.read_bars(list(SAMPLE_TYPES), START, TARGET)[0].first_seen_at == original[0].first_seen_at
    assert Path(first["run_directory"], "result.json").is_file()


def test_failed_calendar_never_calls_history_or_generates_fake_data(tmp_path):
    client = FakeClient(fail_calendar=True)
    result = run(tmp_path, client=client, start_date=START)
    assert result["status"] == "failed"
    assert len(client.calls) == 1
    assert result["database_row_count"] == 0
    assert result["actual_latest_data_date"] is None
    assert result["not_attempted_count"] == 7


def test_empty_history_is_partial_and_next_run_resumes(tmp_path):
    first = run(tmp_path, start_date=START, client=FakeClient(fail_history_symbol="sh.600036"))
    assert first["status"] == "partial"
    assert first["database_row_count"] == 18
    assert first["coverage"]["unknown_missing_count"] == 3
    second = run(tmp_path, start_date=START)
    assert second["status"] == "ok"
    assert second["database_row_count"] == 21
    assert sum(item["inserted"] for item in second["items"]) == 3


def test_permission_denial_stops_further_history_requests(tmp_path):
    client = FakeClient(fail_history_symbol="sh.600036", historical_status="permission_denied")
    result = run(tmp_path, start_date=START, client=client)
    histories = [p for op, p in client.calls if op == "history" and p["start_date"] != p["end_date"]]
    assert len(histories) == 3
    assert result["status"] == "partial"
    assert result["not_attempted_count"] == 4


def test_stale_probe_blocks_all_history_not_renamed_as_target_date(tmp_path):
    client = FakeClient(stale_symbol="sh.600036")
    result = run(tmp_path, client=client, start_date=START)
    assert result["status"] == "failed"
    assert result["single_day_probe_passed"] is False
    assert result["database_row_count"] == 0
    assert any(item["latest_date"] == "2026-09-07" for item in result["items"])
    assert not any(op == "history" and p["start_date"] != p["end_date"] for op, p in client.calls)


@pytest.mark.parametrize(("requested", "status"), [(date(2026, 9, 6), "non_trading_day"), (NOW.date(), "not_ready")])
def test_non_trading_and_not_yet_updated_dates_are_explicit(tmp_path, requested, status):
    client = FakeClient()
    result = run(tmp_path, "doctor", target_date=requested, client=client)
    assert result["status"] == status
    assert result["actual_latest_data_date"] is None
    assert len(client.calls) == 1
    assert result["success_count"] == 0


def test_default_date_comes_from_calendar_not_weekdays(tmp_path):
    result = run(tmp_path, "doctor", target_date=None)
    assert result["target_trade_date"] == "2026-09-08"


def test_window_planner_repairs_old_holes_and_only_overlaps_tail():
    days = [date(2026, 8, 20) + timedelta(days=i) for i in range(10)]
    assert incremental_windows(days, set(days)) == [(days[-3], days[-1])]
    assert incremental_windows(days, set(days[1:])) == [(days[0], days[0]), (days[-3], days[-1])]
    assert incremental_windows(days, set(days), refresh=True) == [(days[0], days[-1])]


def test_check_is_offline_and_exports_stored_values(tmp_path):
    run(tmp_path, start_date=START)
    checked = check_market(start_date=START, target_date=TARGET, database=tmp_path / "research.sqlite3", output_dir=tmp_path / "checks")
    assert checked["status"] == "ok"
    assert checked["network_access"] == "disabled"
    assert checked["coverage"]["target_data_status"] == "current"
    assert checked["csv_record_count"] == 21


def test_m1_rejects_demo_paths_before_writes(tmp_path):
    with pytest.raises(ValueError, match="demo"):
        run_market("collect", target_date=TARGET, client=FakeClient(), now=NOW,
                   database=tmp_path / "demo" / "db.sqlite3", output_dir=tmp_path / "out")
    assert not list(tmp_path.iterdir())


def test_future_and_oversized_requests_fail_before_network(tmp_path):
    client = FakeClient()
    with pytest.raises(ValueError):
        run(tmp_path, client=client, target_date=date(2027, 1, 1))
    with pytest.raises(ValueError):
        run(tmp_path, client=client, start_date=date(2020, 1, 1))
    assert client.calls == []


def test_old_null_field_is_repaired_without_full_refresh(tmp_path, monkeypatch):
    older = date(2026, 9, 1)
    monkeypatch.setattr(__import__(__name__, fromlist=["TRADING"]), "TRADING", TRADING | {older, date(2026, 9, 2)})

    class IncompleteClient(FakeClient):
        def query(self, operation, **parameters):
            response = super().query(operation, **parameters)
            if operation == "history" and parameters["start_date"] != parameters["end_date"]:
                for row in response["rows"]:
                    if row["code"] == "sh.600000" and row["date"] == older.isoformat():
                        row["amount"] = ""
            return response

    first = run(tmp_path, start_date=START, client=IncompleteClient())
    assert first["status"] == "partial"
    assert first["coverage"]["quality_issue_count"] == 1
    complete_client = FakeClient()
    second = run(tmp_path, start_date=START, client=complete_client)
    assert second["status"] == "ok"
    assert second["coverage"]["quality_issue_count"] == 0
    assert any(op == "history" and args["code"] == "sh.600000" and args["start_date"] == older.isoformat()
               and args["end_date"] == older.isoformat() for op, args in complete_client.calls)
    assert second["actual_latest_data_date"] == second["coverage"]["actual_latest_data_date"]


def test_test_client_cannot_write_default_research_paths():
    with pytest.raises(ValueError, match="测试数据库"):
        run_market("collect", target_date=TARGET, client=FakeClient(), now=NOW)


def test_remote_error_is_text_in_csv_and_preserved_in_json(tmp_path):
    import csv

    class ErrorClient(FakeClient):
        def query(self, operation, **parameters):
            response = super().query(operation, **parameters)
            if operation == "basic":
                response.update(ok=False, status="permission_denied", error_msg='=HYPERLINK("https://invalid.example","not executed")')
            return response

    result = run(tmp_path, "doctor", client=ErrorClient())
    directory = Path(result["run_directory"])
    with (directory / "sample_status.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["reason"].startswith("'=HYPERLINK")
    assert result["items"][0]["reason"].startswith("=HYPERLINK")
