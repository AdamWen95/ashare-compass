"""Synthetic M2.1 sample acquisition checks. These never contact BaoStock."""

from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from ashare_daily.m1_settings import SAMPLE_TYPES
from ashare_daily.m2_data import canonical_hash
from ashare_daily.providers.baostock import expected_fields, raw_hash, validate_request
from ashare_daily.sample_data import _missing_windows, collect_sample_data, load_sample_config
from ashare_daily.storage.market import MarketStore


ROOT = Path(__file__).resolve().parents[1]
T = date(2026, 9, 8)
# Explicit artificial calendar: 120 consecutive dates, including weekends.
# It is intentionally not a guessed real exchange calendar.
DATES = [T - timedelta(days=119 - n) for n in range(120)]


@pytest.fixture
def sample_file(tmp_path):
    content = json.loads((ROOT / "config/samples/m21_30.json").read_text(encoding="utf-8"))
    content.update(stock_count=6, sample_id="offline-six", symbol_types=dict(SAMPLE_TYPES))
    path = tmp_path / "samples.json"
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return path


class FakeClient:
    def __init__(self, *, fail_operation=None, failure="permission_denied", empty_symbol=None, wrong_type=None, is_st=False, suspended_missing=False):
        self.calls = []
        self.fail_operation = fail_operation
        self.failure = failure
        self.empty_symbol = empty_symbol
        self.wrong_type = wrong_type
        self.is_st = is_st
        self.suspended_missing = suspended_missing

    def query(self, operation, **parameters):
        self.calls.append((operation, parameters))
        parameters = validate_request(operation, parameters)
        fields = expected_fields(operation, parameters)
        result = {"ok": True, "status": "ok", "error_code": "0", "error_msg": "offline fixture", "fields": fields,
                  "fetched_at": "2026-09-10T12:00:00+08:00", "sdk_version": "offline-test", "parameters": parameters}
        rows = []
        if operation == self.fail_operation:
            result.update(ok=False, status=self.failure, error_code="offline-error", error_msg="synthetic failure")
        elif operation == "calendar":
            begin, end = date.fromisoformat(parameters["start_date"]), date.fromisoformat(parameters["end_date"])
            rows = [{"calendar_date": (begin + timedelta(days=n)).isoformat(), "is_trading_day": "1" if begin + timedelta(days=n) in DATES else "0"} for n in range((end - begin).days + 1)]
        elif operation == "basic":
            symbol = parameters["code"]
            rows = [{"code": symbol, "code_name": "OFFLINE 合成技术样本", "ipoDate": "2000-01-01", "outDate": "", "status": "1",
                     "type": self.wrong_type if self.wrong_type and symbol == "sh.600000" else "2" if symbol == "sh.000001" else "1"}]
        else:
            symbol = parameters["code"]
            if symbol != self.empty_symbol:
                for day in DATES:
                    if parameters["start_date"] <= day.isoformat() <= parameters["end_date"]:
                        row = dict(zip(fields[:9], [day.isoformat(), symbol, "10", "12", "9", "11", "10", "10000000", "50000000"], strict=True))
                        if parameters["security_type"] == "stock":
                            row.update(adjustflag="2" if parameters["adjustment_mode"] == "forward_adjusted" else "3", tradestatus="1", isST="1" if self.is_st else "0")
                            if self.suspended_missing and day == T:
                                row.update(volume="", amount="", tradestatus="0", isST="1")
                        rows.append(row)
        result["rows"] = rows
        result["raw_hash"] = raw_hash(fields, rows)
        return result


def collect(sample_file, tmp_path, client, **kwargs):
    return collect_sample_data(sample_file, target_date=T, database=tmp_path / "market.sqlite3",
                               output_dir=tmp_path / "evidence", adjusted_dir=tmp_path / "adjusted",
                               reuse_adjusted_dirs=[], client=client, **kwargs)


@pytest.mark.parametrize("hour,allowed,accept", [(15, True, False), (16, False, False), (16, True, True), (21, True, True)])
def test_m4_current_day_is_explicit_and_after_cutoff(sample_file, tmp_path, monkeypatch, hour, allowed, accept):
    """Synthetic clock only: this never declares a weekday to be a trade day."""
    import ashare_daily.sample_data as module
    from ashare_daily.market_schemas import SHANGHAI

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(T, datetime.min.time(), SHANGHAI).replace(hour=hour)

    monkeypatch.setattr(module, "datetime", FixedClock)
    client = FakeClient(fail_operation="calendar")
    if not accept:
        with pytest.raises(ValueError, match="已完成"):
            collect(sample_file, tmp_path, client, allow_current_day=allowed)
        assert client.calls == []
    else:
        result = collect(sample_file, tmp_path, client, allow_current_day=allowed)
        assert result["status"] == "failed"  # A calendar failure is not guessed away.
        assert len(client.calls) == 1 and client.calls[0][0] == "calendar"


def test_fixed_balanced_selection_preserves_legacy_and_is_nested():
    small = load_sample_config(ROOT / "config/samples/m21_30.json")
    large = load_sample_config(ROOT / "config/samples/m21_100.json")
    assert set(SAMPLE_TYPES) <= set(small["symbol_types"]) < set(large["symbol_types"])
    for content, per_exchange in ((small, 15), (large, 50)):
        for exchange in ("sh", "sz"):
            assert sum(symbol.startswith(exchange) and kind == "stock" for symbol, kind in content["symbol_types"].items()) == per_exchange
        assert sum(kind == "index" for kind in content["symbol_types"].values()) == 1
        assert content["config_hash"] == load_sample_config(content["config_path"])["config_hash"]


@pytest.mark.parametrize("change", ["duplicate", "wrong_selection", "non_mainboard", "wrong_benchmark", "unknown_method", "too_large"])
def test_invalid_pool_never_silently_changes_sample(sample_file, change):
    data = json.loads(sample_file.read_text(encoding="utf-8"))
    if change == "duplicate":
        data["candidate_pools"]["sh"].append("sh.600000")
    elif change == "wrong_selection":
        data["symbol_types"].pop("sh.600000")
    elif change == "non_mainboard":
        data["candidate_pools"]["sh"][0] = "sh.688001"
    elif change == "wrong_benchmark":
        data["benchmark"]["symbol"] = "sz.399001"
    elif change == "unknown_method":
        data["selection_method"]["algorithm"] = "best_future_return"
    else:
        data["stock_count"] = 5000
    sample_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_sample_config(sample_file)


@pytest.mark.parametrize("malformed", [[], None, "symbols"])
def test_non_object_sample_config_is_rejected_cleanly(sample_file, malformed):
    sample_file.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        load_sample_config(sample_file)


@pytest.mark.parametrize("field,value", [("selection_method", None), ("candidate_pools", None), ("preserve_symbols", [{}])])
def test_malformed_selector_fields_are_rejected_cleanly(sample_file, field, value):
    content = json.loads(sample_file.read_text(encoding="utf-8"))
    content[field] = value
    sample_file.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError):
        load_sample_config(sample_file)


def test_missing_window_does_not_refresh_complete_fixed_history():
    assert _missing_windows(DATES, set(DATES)) == []
    assert _missing_windows(DATES, set(DATES) - {DATES[0]}) == [(DATES[0], DATES[0])]


def test_collection_uses_calendar_freezes_original_responses_and_repeats_without_network(sample_file, tmp_path):
    first_client = FakeClient()
    first = collect(sample_file, tmp_path, first_client)
    assert first["status"] == "ok"
    assert first["verification_kind"] == "offline_test"
    assert first["stock_market_success_count"] == 6
    assert first["market_success_count"] == 7
    assert first["database_rows_after"] == 840
    assert first["coverage"]["unknown_missing_count"] == 0
    assert first["start_date"] == DATES[0].isoformat()
    assert all(item["unadjusted_rows"] == item["adjusted_rows"] == 120 for item in first["items"])
    bundle = json.loads(Path(first["adjusted_manifest"]).read_text(encoding="utf-8"))
    assert len(bundle["series"]) == 7
    assert all(Path(series["response_path"]).is_file() for series in bundle["series"].values())
    assert bundle["manifest_hash"] == canonical_hash({key: value for key, value in bundle.items() if key != "manifest_hash"})
    second_client = FakeClient()
    second = collect(sample_file, tmp_path, second_client)
    assert second["status"] == "ok"
    assert second_client.calls == []
    assert second["database_rows_before"] == second["database_rows_after"] == 840
    assert second["adjusted_reused_series_count"] == 7
    assert first["run_directory"] != second["run_directory"]
    assert Path(first["adjusted_manifest"]).is_file()


def test_wrong_basic_type_fails_without_replacement(sample_file, tmp_path):
    result = collect(sample_file, tmp_path, FakeClient(wrong_type="5"))
    assert result["status"] == "partial"
    item = next(item for item in result["items"] if item["symbol"] == "sh.600000")
    assert item["status"] == "failed"
    assert "证券类型不匹配" in item["reason"]
    assert len(result["items"]) == 7
    assert result["stock_market_success_count"] == 5


def test_st_is_preserved_as_fact_for_eligibility_not_removed_during_collection(sample_file, tmp_path):
    result = collect(sample_file, tmp_path, FakeClient(is_st=True))
    assert result["status"] == "ok"
    store = MarketStore(tmp_path / "market.sqlite3")
    assert store.read_bars(["sh.600000"], T, T)[0].is_st is True


def test_empty_response_stays_failed_and_is_not_zero_filled(sample_file, tmp_path):
    result = collect(sample_file, tmp_path, FakeClient(empty_symbol="sh.600000"))
    assert result["status"] == "partial"
    assert next(item for item in result["items"] if item["symbol"] == "sh.600000")["unadjusted_rows"] == 0
    assert MarketStore(tmp_path / "market.sqlite3").read_bars(["sh.600000"], DATES[0], T) == []


def test_suspended_missing_amount_keeps_known_states_and_marks_quality_gap(sample_file, tmp_path):
    result = collect(sample_file, tmp_path, FakeClient(suspended_missing=True))
    assert result["status"] == "partial"
    assert result["stock_market_success_count"] == 0
    assert result["stock_market_returned_count"] == result["stock_market_full_window_count"] == 6
    original = MarketStore(tmp_path / "market.sqlite3").read_bars(["sh.600000"], T, T)[0]
    assert original.is_st is True and original.tradestatus is False
    assert original.volume_shares is None and original.amount_cny is None
    bundle = json.loads(Path(result["adjusted_manifest"]).read_text(encoding="utf-8"))
    assert bundle["status"] == "partial"
    assert len(bundle["series"]["sh.600000"]["bars"]) == 120
    assert bundle["series"]["sh.600000"]["bars"][-1]["amount_cny"] is None
    assert bundle["series"]["sh.600000"]["issues"]
    assert any(failure["symbol"] == "sh.600000" for failure in bundle["failures"])


def test_permission_failure_stops_following_source_requests(sample_file, tmp_path):
    client = FakeClient(fail_operation="basic")
    result = collect(sample_file, tmp_path, client)
    assert result["status"] == "partial"
    assert [op for op, _ in client.calls] == ["calendar", "basic"]
    assert sum(item["status"] == "not_attempted" for item in result["items"]) == 6
    assert result["market_success_count"] == 0


def test_calendar_failure_never_invents_weekdays(sample_file, tmp_path):
    client = FakeClient(fail_operation="calendar")
    result = collect(sample_file, tmp_path, client)
    assert result["status"] == "failed"
    assert result["database_rows_after"] == 0
    assert len(client.calls) == 1
    assert result["failures"][0]["stage"] == "calendar"


def test_corrupt_raw_adjusted_cache_is_not_reused(sample_file, tmp_path):
    first = collect(sample_file, tmp_path, FakeClient())
    bundle = json.loads(Path(first["adjusted_manifest"]).read_text(encoding="utf-8"))
    path = Path(bundle["series"]["sh.600000"]["response_path"])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["result"]["rows"][0]["close"] = "999"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    second_client = FakeClient()
    second = collect(sample_file, tmp_path, second_client)
    assert second["status"] == "ok"
    assert second["adjusted_reused_series_count"] == 6
    assert len(second_client.calls) == 1
    assert second_client.calls[0][1]["adjustment_mode"] == "forward_adjusted"


def test_offline_client_cannot_write_real_database(sample_file, tmp_path):
    with pytest.raises(ValueError, match="离线客户端"):
        collect_sample_data(sample_file, target_date=T, client=FakeClient(), output_dir=tmp_path / "out", adjusted_dir=tmp_path / "adj")


def test_offline_database_marker_persists_and_live_client_cannot_downgrade_it(sample_file, tmp_path):
    from ashare_daily.providers.baostock import BaoStockClient
    collect(sample_file, tmp_path, FakeClient())
    with sqlite3.connect(tmp_path / "market.sqlite3") as connection:
        assert dict(connection.execute("SELECT key,value FROM market_metadata"))["verification_kind"] == "offline_test"
    with pytest.raises(ValueError, match="offline_test"):
        collect(sample_file, tmp_path, BaoStockClient())


def test_fake_client_cannot_mark_existing_real_rows_as_test(sample_file, tmp_path):
    collect(sample_file, tmp_path, FakeClient())
    with sqlite3.connect(tmp_path / "market.sqlite3") as connection:
        connection.execute("DELETE FROM market_metadata WHERE key='verification_kind'")
    with pytest.raises(ValueError, match="已有真实"):
        collect(sample_file, tmp_path, FakeClient())


@pytest.mark.parametrize("points", [20, 119, 261, True])
def test_cannot_reduce_indicator_requirements(sample_file, tmp_path, points):
    with pytest.raises(ValueError, match="120"):
        collect(sample_file, tmp_path, FakeClient(), history_points=points)


def test_thirty_to_hundred_collects_only_new_symbols_and_batches_are_bounded(tmp_path):
    first = collect(ROOT / "config/samples/m21_30.json", tmp_path, FakeClient())
    assert first["status"] == "ok"
    assert first["stock_market_success_count"] == 30
    second_client = FakeClient()
    second = collect(ROOT / "config/samples/m21_100.json", tmp_path, second_client)
    assert second["status"] == "ok"
    assert second["stock_market_success_count"] == 100
    assert second["database_rows_after"] == 101 * 120
    assert second["database_rows_after"] - second["database_rows_before"] == 70 * 120
    assert second["adjusted_reused_series_count"] == 31
    assert all(len(batch["symbols"]) <= 10 for batch in second["adjusted_new_batches"])
    first_symbols = set(first["sample_config"]["symbol_types"])
    assert all(parameters.get("code") not in first_symbols for _, parameters in second_client.calls)
    assert len(second["items"]) == second["market_success_count"] + second["failure_count"] + second["not_attempted_count"]
