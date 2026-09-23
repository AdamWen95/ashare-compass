"""Synthetic F2 fixtures only; no networking and no writes to research paths."""
from datetime import date, timedelta
from hashlib import sha256
import json
import sqlite3

import pytest

from ashare_daily.market_foundation import F2MarketStore, QUALITY_RULES_VERSION, normalize_baostock_rows, plan_history
from ashare_daily.providers.baostock import HISTORY_STOCK_FIELDS, base_result, raw_hash

DAY = "2026-09-10"
PREVIOUS = "2026-09-09"
STAMP = "2026-09-10T21:00:00+08:00"
SYMBOL = "sh.688001"  # The identity comes from F1, not a main-board prefix test.
SID = "synthetic-f1-security"


def row(**updates):
    return {"date": DAY, "code": SYMBOL, "open": "10", "high": "11", "low": "9", "close": "10",
            "preclose": "10", "volume": "12345", "amount": "123450", "adjustflag": "3",
            "tradestatus": "1", "isST": "0", **updates}


def normalized(rows=None, **updates):
    options = {"security_id": SID, "symbol": SYMBOL, "start_date": DAY, "end_date": DAY,
               "trading_dates": [DAY], **updates}
    return normalize_baostock_rows([row()] if rows is None else rows, **options)


def response(rows=None, *, adjusted=False, stamp=STAMP, start=DAY, end=DAY):
    mode = "forward_adjusted" if adjusted else "unadjusted"
    params = {"code": SYMBOL, "start_date": start, "end_date": end,
              "security_type": "stock", "adjustment_mode": mode}
    result = base_result("history_f2" if adjusted else "history", params)
    data = rows if rows is not None else [row(adjustflag="2" if adjusted else "3")]
    result.update(ok=True, status="ok" if data else "empty_confirmed", error_code="0", rows=data,
                  fields=list(HISTORY_STOCK_FIELDS), fetched_at=stamp,
                  raw_hash=raw_hash(HISTORY_STOCK_FIELDS, data), provenance_mode="offline_test",
                  login={"ok": True, "error_code": "0", "error_msg": "success"})
    return result


@pytest.fixture
def store(tmp_path):
    return F2MarketStore(tmp_path / "offline_test" / "market.sqlite3", mode="offline_test")


def save(store, data=None, *, days=None, **updates):
    data = data if data is not None else response()
    content = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf8")
    digest = sha256(content).hexdigest()
    path = store.path.parent / (digest + ".json")
    if not path.exists():
        path.write_bytes(content)
    args = {"security_id": SID, "symbol": SYMBOL, "scope": "sse_szse_a",
            "universe_snapshot_id": "fixture-universe-v1", "response": data,
            "source_response_path": path, "source_response_hash": digest,
            "trading_dates": days or [DAY], "provenance_mode": "offline_test",
            "adjustment_mode": data["parameters"]["adjustment_mode"], **updates}
    return store.save_batch(**args)


def count(store, table):
    with sqlite3.connect(store.path) as connection:
        return connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def test_units_ratios_and_five_board_symbol_independence():
    data = normalized([row(turn="2.5", pctChg="1.25")])
    fact = data["records"][0]
    assert fact["volume_shares"] == 12345 and fact["amount_cny"] == "123450"
    assert fact["turnover_ratio"] == "0.025" and fact["provider_change_ratio"] == "0.0125"
    assert fact["reference_change_ratio"] == "0"
    assert fact["after_hours_volume_inclusion"] == "unverified_no_addition"
    assert data["complete"]


@pytest.mark.parametrize("change,reason", [
    ({"close": "NaN"}, "invalid_number"), ({"amount": "Infinity"}, "invalid_number"),
    ({"open": "0"}, "price_must_be_positive"), ({"volume": "12.5"}, "invalid_number"),
    ({"volume": "-1"}, "invalid_number"), ({"amount": "-1"}, "invalid_number"),
    ({"high": "8"}, "ohlc_range_invalid"), ({"close": "12"}, "ohlc_range_invalid"),
    ({"code": "sz.000001"}, "symbol_mismatch"), ({"date": PREVIOUS}, "date_not_in_verified_requested_calendar"),
    ({"adjustflag": "2"}, "adjustment_mode_mismatch"), ({"isST": "false"}, "invalid_status"),
    ({"volume": 12345}, "raw_fields_must_be_strings"), ({"tradestatus": "0"}, "suspension_volume_amount_conflict"),
])
def test_bad_rows_are_quarantined_and_remain_missing(change, reason):
    result = normalized([row(**change)])
    assert not result["quote_complete"] and not result["records"]
    assert result["missing_dates"] == [DAY]
    assert reason in result["quality_issues"][0]["reason"]


def test_missing_prices_are_null_and_need_repair_not_filled():
    result = normalized([row(close="", amount="")])
    assert result["records"][0]["close"] is None
    assert result["records"][0]["amount_cny"] is None
    assert "missing_close" in result["records"][0]["quality_flags"]
    assert not result["quote_complete"]


def test_unknown_status_is_not_false_and_remains_in_denominator(store):
    result = save(store, response([row(tradestatus="", isST="")]))
    assert not result["quality"]["quote_complete"] and not result["quality"]["status_complete"]
    assert result["quality"]["status_unknown_dates"] == [DAY]
    fact = store.read_bars(SID, DAY, DAY)[0]
    assert fact["tradestatus"] is None and fact["is_st"] is None
    assert result["quality"]["valid_quote_dates"] == []
    assert store.stored_dates(SID, DAY, DAY) == set()


def test_st_unknown_does_not_discard_verified_trading_price_facts(store):
    result = save(store, response([row(isST="")]))
    assert result["quality"]["quote_complete"]
    assert not result["quality"]["status_complete"]
    assert result["quality"]["status_unknown_dates"] == [DAY]
    assert store.stored_dates(SID, DAY, DAY) == {DAY}


def test_evidenced_suspension_is_separate_from_missing_and_not_a_fake_bar():
    result = normalized([row(tradestatus="0", volume="", amount="", open="", high="", low="", close="", preclose="")])
    assert result["suspended_dates"] == [DAY]
    assert result["valid_quote_dates"] == [] and result["quote_complete"]
    assert result["records"][0]["close"] is None
    assert "missing_volume_shares" in result["records"][0]["quality_flags"]
    assert "missing_amount_cny" in result["records"][0]["quality_flags"]
    assert not result["complete"]  # Existing field gate remains stricter than Q coverage.
    absent = normalized([])
    assert absent["suspended_dates"] == [] and absent["missing_dates"] == [DAY]


def test_duplicate_date_rejects_both_rows_and_keeps_missing_denominator():
    result = normalized([row(), row(close="10.1")])
    assert result["raw_row_count"] == 2 and result["records"] == []
    assert len(result["quality_issues"]) == 2 and result["missing_dates"] == [DAY]


def test_ex_right_reference_change_is_a_review_fact_not_fixed_return_failure():
    result = normalized([row(date=PREVIOUS, close="10"), row(open="5", close="5", high="6", low="4", preclose="5")],
                        start_date=PREVIOUS, trading_dates=[PREVIOUS, DAY])
    assert result["quote_complete"]
    assert result["review_flags"] == [{"date": DAY, "reason": "reference_preclose_differs_from_previous_close"}]


def test_320_trading_day_plan_splits_raw_natural_day_limit_without_truncation():
    start = date(2024, 1, 1)
    calendar = {(start + timedelta(days=index)).isoformat(): index % 2 == 0 for index in range(639)}
    target = start + timedelta(days=638)
    result = plan_history(target_date=target, calendar=calendar, calendar_verified=True,
                          listing_date=start, recheck_days=0)
    assert result["plan_verified"] and result["expected_count"] == 320
    assert len(result["raw_ranges"]) == 2
    assert all((date.fromisoformat(part["end_date"]) - date.fromisoformat(part["start_date"])).days <= 365 for part in result["raw_ranges"])
    assert result["raw_ranges"][-1]["end_date"] == target.isoformat()


def test_new_listing_uses_evidenced_available_history_not_320_eligibility_gate():
    result = plan_history(target_date=DAY, calendar={PREVIOUS: True, DAY: True}, calendar_verified=True,
                          listing_date=PREVIOUS, recheck_days=0)
    assert result["plan_verified"] and result["expected_count"] == 2


@pytest.mark.parametrize("updates,blocker", [
    ({"calendar_verified": False}, "calendar_unverified"),
    ({"listing_date": None}, "listing_date_unknown"),
    ({"calendar": {PREVIOUS: True}}, "target_calendar_missing"),
    ({"calendar": {PREVIOUS: True, DAY: False}}, "target_not_trading_day"),
    ({"listing_date": "2000-01-01"}, "calendar_window_insufficient"),
])
def test_calendar_unknown_closed_or_short_history_never_guesses(updates, blocker):
    args = {"target_date": DAY, "calendar": {PREVIOUS: True, DAY: True}, "calendar_verified": True,
            "listing_date": PREVIOUS, "recheck_days": 0, **updates}
    result = plan_history(**args)
    assert not result["plan_verified"] and blocker in result["blockers"]


def test_calendar_gaps_and_string_flags_are_not_workdays():
    result = plan_history(target_date=DAY, calendar={"2026-09-08": True, DAY: True},
                          calendar_verified=True, listing_date="2026-09-08", recheck_days=0)
    assert "calendar_natural_day_gap" in result["blockers"]
    with pytest.raises(ValueError, match="boolean"):
        plan_history(target_date=DAY, calendar={DAY: "1"}, calendar_verified=True, listing_date=DAY)


def test_resume_only_requests_missing_and_explicit_recheck_sessions():
    result = plan_history(target_date=DAY, calendar={PREVIOUS: True, DAY: True}, calendar_verified=True,
                          listing_date=PREVIOUS, stored_dates=[PREVIOUS, DAY], recheck_days=0)
    assert result["raw_ranges"] == [] and not result["missing_dates"]
    result = plan_history(target_date=DAY, calendar={PREVIOUS: True, DAY: True}, calendar_verified=True,
                          listing_date=PREVIOUS, stored_dates=[PREVIOUS, DAY], recheck_days=1)
    assert result["raw_ranges"] == [{"start_date": DAY, "end_date": DAY}]


def test_exact_replay_is_idempotent_and_correction_keeps_frozen_fact(store):
    first = save(store)
    original = store.read_bars(SID, DAY, DAY)[0]
    repeat = save(store)
    assert repeat["unchanged"] == 1 and repeat["batch_id"] == first["batch_id"]
    corrected = save(store, response([row(close="10.5")], stamp="2026-09-10T22:00:00+08:00"))
    assert corrected["updated"] == 1
    assert count(store, "f2_bar_versions") == 2 and count(store, "f2_bar_current") == 1
    with sqlite3.connect(store.path) as connection:
        frozen = connection.execute("SELECT payload_json FROM f2_bar_versions WHERE fact_hash=?", (original["fact_hash"],)).fetchone()[0]
    assert json.loads(frozen)["close"] == "10"
    assert store.get_bar_version(SID, DAY, original["fact_hash"]) == original
    assert store.read_bars(SID, DAY, DAY)[0]["close"] == "10.5"


def test_business_fact_hash_ignores_fetch_time_decimal_spelling_and_request_range(store):
    save(store)
    result = save(store, response([row(close="10.00")], stamp="2026-09-10T22:00:00+08:00", start=PREVIOUS), days=[DAY])
    assert result["unchanged"] == 1 and count(store, "f2_bar_versions") == 1
    assert count(store, "f2_bar_observations") == 2


def test_older_or_same_time_correction_rolls_back_batch(store):
    save(store)
    for stamp in ("2026-09-10T20:00:00+08:00", STAMP):
        with pytest.raises(ValueError, match="older observation|same-time"):
            save(store, response([row(close="10.5")], stamp=stamp))
    assert count(store, "f2_bar_versions") == 1 and count(store, "f2_batches") == 1


def test_invalid_row_audit_preserved_without_losing_good_rows(store):
    data = response([row(date=PREVIOUS), row(close="NaN")], start=PREVIOUS)
    result = save(store, data, days=[PREVIOUS, DAY])
    assert result["inserted"] == 1 and result["quality"]["missing_dates"] == [DAY]
    assert count(store, "f2_batches") == 1
    assert store.stored_dates(SID, PREVIOUS, DAY) == {PREVIOUS}


def test_missing_field_fact_is_preserved_but_not_marked_completed_for_resume(store):
    save(store, response([row(close="")]))
    assert len(store.read_bars(SID, DAY, DAY)) == 1
    assert store.stored_dates(SID, DAY, DAY) == set()


def test_evidenced_suspension_does_not_require_repeated_missing_volume_download(store):
    result = save(store, response([row(tradestatus="0", volume="", amount="")]))
    assert result["quality"]["quote_complete"]
    assert not result["quality"]["complete"]
    assert store.stored_dates(SID, DAY, DAY) == {DAY}


def test_adjustment_window_is_whole_versioned_and_never_raw_current(store):
    first = save(store, response(adjusted=True))
    window = store.get_adjustment_window(first["window_id"])
    assert window["anchor_kind"] == "provider_current_at_fetch"
    assert window["point_in_time_adjustment_verified"] is False
    assert window["observations"][0]["source_file_hash"] == first["source_file_hash"]
    assert store.read_bars(SID, DAY, DAY) == []
    repeat = save(store, response(adjusted=True, stamp="2026-09-10T22:00:00+08:00"))
    assert repeat["window_id"] == first["window_id"]
    changed = save(store, response([row(adjustflag="2", close="10.5")], adjusted=True, stamp="2026-09-10T23:00:00+08:00"))
    assert changed["window_id"] != first["window_id"]
    assert store.get_adjustment_window(first["window_id"])["records"] == window["records"]
    assert count(store, "f2_adjustment_windows") == 2


def test_incomplete_adjusted_response_does_not_create_usable_window(store):
    result = save(store, response(adjusted=True, start=PREVIOUS), days=[PREVIOUS, DAY])
    assert result["window_id"] is None and result["quality"]["missing_dates"] == [PREVIOUS]
    assert count(store, "f2_batches") == 1 and count(store, "f2_adjustment_windows") == 0


def test_forward_adjustment_cannot_enter_appendable_legacy_history(store):
    data = response(adjusted=True)
    data["operation"] = "history"
    with pytest.raises(ValueError, match="single complete"):
        save(store, data)


def test_adjusted_segment_cannot_cover_a_larger_required_window(store):
    with pytest.raises(ValueError, match="segment"):
        save(store, response(adjusted=True), days=[PREVIOUS, DAY])
    assert count(store, "f2_adjustment_windows") == 0


@pytest.mark.parametrize("mutation", [
    lambda data: data["login"].update(ok=False),
    lambda data: data.update(provider="another_source"),
    lambda data: data.update(fetched_at="2100-01-01T21:00:00+08:00"),
])
def test_provider_login_or_future_observation_cannot_be_accepted(store, mutation):
    data = response()
    mutation(data)
    with pytest.raises(ValueError):
        save(store, data)
    assert count(store, "f2_batches") == 0


def test_source_file_raw_hash_and_provenance_are_required(store):
    with pytest.raises(ValueError, match="file/hash"):
        save(store, source_response_hash="0" * 64)
    data = response()
    data["raw_hash"] = "0" * 64
    with pytest.raises(ValueError, match="raw_hash"):
        save(store, data)
    with pytest.raises(ValueError, match="provenance"):
        save(store, provenance_mode="online")
    data = response()
    data["provenance_mode"] = "online"
    with pytest.raises(ValueError, match="provenance"):
        save(store, data)
    assert count(store, "f2_batches") == 0


def test_fixtures_cannot_open_or_contaminate_research_database(tmp_path):
    with pytest.raises(ValueError, match="research"):
        F2MarketStore(tmp_path / "research" / "market.sqlite3", mode="offline_test")
    target = F2MarketStore(tmp_path / "isolated.sqlite3", mode="research")
    with pytest.raises(ValueError, match="provenance"):
        save(target)
    with pytest.raises(ValueError, match="provenance"):
        F2MarketStore(target.path, mode="offline_test")


def test_additive_schema_preserves_legacy_tables_and_sqlite_backup(tmp_path):
    from ashare_daily.storage.market import MarketStore
    path = tmp_path / "offline_test" / "market.sqlite3"
    MarketStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO market_metadata VALUES('verification_kind','offline_test')")
        before = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    target = F2MarketStore(path, mode="offline_test")
    save(target)
    with sqlite3.connect(path) as connection:
        after = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'f2_%' ORDER BY name").fetchall()
        assert after == before
        with sqlite3.connect(tmp_path / "restored.sqlite3") as destination:
            connection.backup(destination)
            assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert destination.execute("SELECT COUNT(*) FROM f2_bar_versions").fetchone()[0] == 1
            assert destination.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0] == 0


def test_migration_error_rolls_back_all_f2_schema_changes(tmp_path):
    from ashare_daily.storage.market import MarketStore
    path = tmp_path / "offline_test" / "market.sqlite3"
    MarketStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO market_metadata VALUES('verification_kind','offline_test')")
        connection.execute("CREATE TABLE f2_bar_versions(incompatible TEXT)")
        before = connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    with pytest.raises(sqlite3.OperationalError):
        F2MarketStore(path, mode="offline_test")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == before


def test_corrupt_fact_or_window_cannot_be_read_as_verified(store):
    save(store)
    window = save(store, response(adjusted=True))["window_id"]
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER f2_bars_no_update")
        connection.execute("UPDATE f2_bar_versions SET payload_json='{}'")
        connection.execute("DROP TRIGGER f2_windows_no_update")
        connection.execute("UPDATE f2_adjustment_windows SET payload_json='{}'")
    with pytest.raises(ValueError, match="hash"):
        store.read_bars(SID, DAY, DAY)
    with pytest.raises(ValueError, match="hash"):
        store.get_adjustment_window(window)


def test_quality_rule_upgrade_preserves_unversioned_batch_and_is_business_idempotent(store):
    # Seed the actual old layout without calling the new writer or changing any
    # old row. This models a batch archived before quality versioning existed.
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = lambda value: sha256(canonical(value).encode("utf8")).hexdigest()
    data = response()
    content = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf8")
    file_hash = sha256(content).hexdigest()
    source = store.path.parent / (file_hash + ".json")
    source.write_bytes(content)
    old_quality = normalized()
    old_quality.pop("quality_rules_version")
    old_quality.pop("trading_status_unknown_dates")
    fact = old_quality["records"][0]
    fact_hash = digest(fact)
    old_provenance = {"provider": "baostock", "mode": "offline_test", "provenance_mode": "offline_test",
                      "scope": "sse_szse_a", "universe_snapshot_id": "fixture-universe-v1", "parameters": data["parameters"],
                      "sdk_version": data["sdk_version"], "fetched_at": STAMP, "anchor_kind": None,
                      "point_in_time_adjustment_verified": False}
    old_id = "f2-batch-" + digest({"security_id": SID, "file_hash": file_hash, "scope": "sse_szse_a",
                                  "universe_snapshot_id": "fixture-universe-v1"})[:32]
    with sqlite3.connect(store.path) as connection:
        connection.execute("INSERT INTO f2_batches VALUES(?,?,?,?,?,?,?,?,?,?)", (
            old_id, SID, "sse_szse_a", "unadjusted", STAMP, str(source), file_hash, data["raw_hash"],
            canonical(old_quality), canonical(old_provenance)))
        connection.execute("INSERT INTO f2_bar_versions VALUES(?,?,?,?,?,?)", (SID, "baostock", DAY, fact_hash, STAMP, canonical(fact)))
        connection.execute("INSERT INTO f2_bar_current VALUES(?,?,?,?,?)", (SID, "baostock", DAY, fact_hash, STAMP))
        connection.execute("INSERT INTO f2_bar_observations VALUES(?,?,?)", (old_id, DAY, fact_hash))
        old_row = connection.execute("SELECT * FROM f2_batches WHERE batch_id=?", (old_id,)).fetchone()
    assert store.get_bar_version(SID, DAY, fact_hash)["close"] == "10"
    revised = save(store, data)
    assert revised["batch_id"] != old_id and revised["quality"]["quality_rules_version"] == QUALITY_RULES_VERSION
    assert revised["unchanged"] == 1 and revised["inserted"] == revised["updated"] == 0
    assert count(store, "f2_bar_versions") == 1 and count(store, "f2_batches") == 2
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT * FROM f2_batches WHERE batch_id=?", (old_id,)).fetchone() == old_row
        provenance = json.loads(connection.execute("SELECT provenance_json FROM f2_batches WHERE batch_id=?", (revised["batch_id"],)).fetchone()[0])
        assert provenance["quality_rules_version"] == QUALITY_RULES_VERSION
        before = tuple(connection.iterdump())
    repeated = save(store, data)
    assert repeated["batch_id"] == revised["batch_id"] and repeated["unchanged"] == 1
    with sqlite3.connect(store.path) as connection:
        assert tuple(connection.iterdump()) == before
    # Explicit reuse of an old identifier is still a conflict, never an overwrite.
    with pytest.raises(ValueError, match="batch_id conflicts"):
        save(store, data, batch_id=old_id)
    assert count(store, "f2_batches") == 2


def test_future_quality_version_adds_only_audit_batch_for_identical_facts(store, monkeypatch):
    first = save(store)
    monkeypatch.setattr("ashare_daily.market_foundation.QUALITY_RULES_VERSION", "synthetic-future-quality-v3")
    second = save(store)
    assert second["batch_id"] != first["batch_id"]
    assert second["quality"]["quality_rules_version"] == "synthetic-future-quality-v3"
    assert second["unchanged"] == 1 and count(store, "f2_bar_versions") == 1
    assert count(store, "f2_batches") == 2
