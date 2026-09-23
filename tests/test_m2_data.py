"""M2 data preparation uses synthetic fixtures; live BaoStock evidence is separate."""

from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ashare_daily.m2_data import (
    AdjustedDataError, canonical_hash, normalize_adjusted_response, prepare_adjusted_data,
)
from ashare_daily.providers.baostock import (
    expected_fields, raw_hash, validate_request,
)
from ashare_daily.providers.baostock_worker import execute_request


DATES = [date(2026, 9, 7), date(2026, 9, 8)]


def response_for(symbol="sh.600000", security_type="stock", **overrides):
    parameters = validate_request("history", {
        "code": symbol, "security_type": security_type,
        "start_date": DATES[0].isoformat(), "end_date": DATES[-1].isoformat(),
        "adjustment_mode": "forward_adjusted" if security_type == "stock" else "unadjusted",
    })
    fields = expected_fields("history", parameters)
    rows = []
    for day in DATES:
        row = dict(zip(fields[:9], [day.isoformat(), symbol, "10", "12", "9", "11", "10", "100000", "50000000"], strict=True))
        if security_type == "stock":
            row.update(adjustflag="2", tradestatus="1", isST="0")
        rows.append(row)
    result = {"ok": True, "status": "ok", "error_code": "0", "error_msg": "success", "rows": rows,
              "fields": fields, "parameters": parameters, "fetched_at": "2026-09-09T13:00:00+08:00", "sdk_version": "offline-test"}
    result.update(overrides)
    result["raw_hash"] = raw_hash(result["fields"], result["rows"])
    return result


def normalize(result, **kwargs):
    return normalize_adjusted_response(result, symbol=kwargs.get("symbol", "sh.600000"),
                                       security_type=kwargs.get("security_type", "stock"), trading_dates=DATES)


class FakeClient:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def query(self, operation, **parameters):
        self.calls.append((operation, parameters))
        if self.failure:
            return {"ok": False, "status": self.failure, "error_code": "test-error", "error_msg": "injected offline failure"}
        return response_for(parameters["code"], parameters["security_type"])


def test_forward_prices_and_original_units_are_explicit():
    actual = normalize(response_for())
    assert actual["adjustment_mode"] == "forward_adjusted"
    assert actual["bars"][0]["close"] == "11"
    assert actual["bars"][0]["amount_cny"] == "50000000"
    assert actual["bars"][0]["volume_shares"] == 100000
    assert actual["bars"][0]["is_st"] is False
    assert actual["issues"] == []
    assert actual["price_unit"] == "CNY"


def test_index_is_native_benchmark_not_a_forward_adjusted_stock():
    actual = normalize(response_for("sh.000001", "index"), symbol="sh.000001", security_type="index")
    assert actual["adjustment_mode"] == "index_native"
    assert actual["price_unit"] == "index_points"
    assert actual["parameters"]["adjustment_mode"] == "unadjusted"
    assert actual["bars"][0]["tradestatus"] is None
    assert actual["bars"][0]["is_st"] is None


def test_stock_worker_passes_explicit_adjustflag_two_and_preserves_m1_default():
    response = response_for()
    class Result:
        error_code = "0"
        error_msg = "success"
        fields = response["fields"]
        def __init__(self):
            self.rows = iter([[row[key] for key in self.fields] for row in response["rows"]])
        def next(self):
            self.row = next(self.rows, None)
            return self.row is not None
        def get_row_data(self):
            return self.row
    calls = []
    sdk = SimpleNamespace(login=lambda: Result(), logout=lambda: Result(),
                          query_history_k_data_plus=lambda *args, **kwargs: (calls.append(kwargs), Result())[1])
    assert execute_request({"operation": "history", "parameters": response["parameters"]}, sdk=sdk)["ok"]
    assert calls[0]["adjustflag"] == "2"
    unadjusted_parameters = {key: value for key, value in response["parameters"].items() if key != "adjustment_mode"}
    execute_request({"operation": "history", "parameters": unadjusted_parameters}, sdk=sdk)
    assert calls[1]["adjustflag"] == "3"


def test_index_forward_adjustment_request_rejected_before_network():
    with pytest.raises(ValueError):
        validate_request("history", {**response_for("sh.000001", "index")["parameters"], "adjustment_mode": "forward_adjusted"})


@pytest.mark.parametrize("field,value", [
    ("adjustflag", "3"), ("code", "sh.600036"), ("date", "2026-09-09"),
    ("close", "NaN"), ("close", "-1"), ("high", "5"),
    ("amount", "-1"), ("volume", "1.5"), ("isST", "unknown"),
])
def test_rejects_invalid_identity_adjustment_dates_and_values(field, value):
    result = response_for()
    result["rows"][0][field] = value
    result["raw_hash"] = raw_hash(result["fields"], result["rows"])
    with pytest.raises(AdjustedDataError):
        normalize(result)


def test_duplicate_rows_are_not_silently_deduplicated():
    result = response_for()
    result["rows"].append(deepcopy(result["rows"][0]))
    result["raw_hash"] = raw_hash(result["fields"], result["rows"])
    with pytest.raises(AdjustedDataError, match="duplicate"):
        normalize(result)


def test_missing_prices_and_statuses_remain_null():
    result = response_for()
    result["rows"][0].update(close="", amount="", isST="", tradestatus="")
    result["raw_hash"] = raw_hash(result["fields"], result["rows"])
    actual = normalize(result)
    assert actual["bars"][0]["close"] is None
    assert actual["bars"][0]["amount_cny"] is None
    assert actual["bars"][0]["tradestatus"] is None
    assert actual["bars"][0]["is_st"] is None
    assert "2026-09-07:missing_close" in actual["issues"]


def test_missing_target_date_is_stale_without_shortening_window():
    result = response_for()
    result["rows"] = result["rows"][:1]
    result["raw_hash"] = raw_hash(result["fields"], result["rows"])
    actual = normalize(result)
    assert actual["actual_latest_data_date"] == "2026-09-07"
    assert "missing_trade_date:2026-09-08" in actual["issues"]
    assert "stale_target_date" in actual["issues"]


def test_suspended_nonzero_trading_values_are_rejected():
    result = response_for()
    result["rows"][0]["tradestatus"] = "0"
    result["raw_hash"] = raw_hash(result["fields"], result["rows"])
    with pytest.raises(AdjustedDataError, match="suspended"):
        normalize(result)


def test_explicit_suspension_zero_is_preserved_without_counting_missing_as_zero():
    result = response_for()
    result["rows"][0].update(tradestatus="0", open="11", high="11", low="11", close="11", amount="0", volume="0")
    result["raw_hash"] = raw_hash(result["fields"], result["rows"])
    actual = normalize(result)
    assert actual["bars"][0]["tradestatus"] is False
    assert actual["bars"][0]["amount_cny"] == "0"
    assert actual["bars"][0]["volume_shares"] == 0


def test_missing_one_price_does_not_hide_invalid_remaining_ohlc():
    result = response_for()
    result["rows"][0].update(close="", high="5")
    result["raw_hash"] = raw_hash(result["fields"], result["rows"])
    with pytest.raises(AdjustedDataError, match="OHLC"):
        normalize(result)


def test_empty_response_retains_missing_dates_and_empty_confirmation():
    result = response_for(rows=[])
    actual = normalize(result)
    assert actual["bars"] == []
    assert actual["actual_latest_data_date"] is None
    assert len([issue for issue in actual["issues"] if issue.startswith("missing_trade_date:")]) == 2
    assert "empty_confirmed" in actual["issues"]


@pytest.mark.parametrize("mutation", [
    lambda response: response.update(raw_hash="0" * 64),
    lambda response: response.update(fetched_at="2026-09-09T13:00:00"),
    lambda response: response["parameters"].update(adjustment_mode="unadjusted"),
])
def test_raw_provenance_and_request_contract_checked(mutation):
    result = response_for()
    mutation(result)
    with pytest.raises(AdjustedDataError):
        normalize(result)


def test_same_full_window_is_immutable_and_new_fetch_does_not_join_versions(tmp_path):
    client = FakeClient()
    first = prepare_adjusted_data({"sh.600000": "stock", "sh.000001": "index"}, DATES, tmp_path, client)
    first_bytes = (Path(first["run_directory"]) / "manifest.json").read_bytes()
    second = prepare_adjusted_data({"sh.600000": "stock", "sh.000001": "index"}, DATES, tmp_path, client)
    assert first["batch_id"] != second["batch_id"]
    assert first_bytes == (Path(first["run_directory"]) / "manifest.json").read_bytes()
    assert all(call[1]["start_date"] == "2026-09-07" and call[1]["end_date"] == "2026-09-08" for call in client.calls)
    assert len(client.calls) == 4
    assert first["verification_kind"] == "offline_test"
    assert first["mode"] == "offline_test"
    assert first["delisting_period_status"] == "unknown"
    assert first["status"] == "ok"
    data = json.loads(first_bytes)
    checksum = data.pop("manifest_hash")
    assert canonical_hash(data) == checksum
    assert not list(tmp_path.rglob("*.sqlite3"))


@pytest.mark.parametrize("status", ["permission_denied", "rate_limited", "schema_changed"])
def test_permission_schema_or_rate_stops_source_without_fake_success(tmp_path, status):
    client = FakeClient(failure=status)
    result = prepare_adjusted_data({"sh.600000": "stock", "sh.600036": "stock"}, DATES, tmp_path, client)
    assert len(client.calls) == 1
    assert result["status"] == "failed"
    assert result["series"] == {}
    assert result["failures"][0]["status"] == status
    assert result["failures"][1]["status"] == "not_attempted"
    assert len(list(Path(result["run_directory"]).rglob("responses/*.json"))) == 1


def test_network_failure_continues_bounded_other_sample_and_saves_error(tmp_path):
    client = FakeClient(failure="timeout")
    result = prepare_adjusted_data({"sh.600000": "stock", "sh.600036": "stock"}, DATES, tmp_path, client)
    assert len(client.calls) == 2
    assert result["success_count"] == 0
    assert result["failure_count"] == 2


@pytest.mark.parametrize("dates", [list(reversed(DATES)), [DATES[0], DATES[0]], [],
                                    [date(2025, 1, 1), date(2026, 9, 8)],
                                    [date(2025, 1, 1) + timedelta(days=offset) for offset in range(261)]])
def test_invalid_calendar_does_not_make_requests_or_write(tmp_path, dates):
    client = FakeClient()
    with pytest.raises(ValueError):
        prepare_adjusted_data({"sh.600000": "stock"}, dates, tmp_path, client)
    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


def test_fake_client_cannot_write_under_real_research_output():
    with pytest.raises(ValueError, match="offline_test"):
        prepare_adjusted_data({"sh.600000": "stock"}, DATES, Path("outputs/research/m2"), FakeClient())
