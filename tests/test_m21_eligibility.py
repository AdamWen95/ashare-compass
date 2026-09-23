"""OFFLINE_TEST: all source text, URLs and securities below are synthetic.

These fixtures demonstrate evidence contracts only; they never constitute a
real security eligibility declaration and are never shipped as import data.
"""

from copy import deepcopy
from datetime import date
import json

import pytest

from ashare_daily.eligibility import load_evidence_bundle, resolve_eligibility


T = date(2026, 9, 8)
SYMBOL = "sh.600001"


def source():
    return {
        "source_id": "offline-source", "name": "OFFLINE_TEST 合成来源",
        "source_url": "https://example.invalid/offline", "access_method": "offline_test",
        "field_meaning": "OFFLINE_TEST 指定生效区间的退市整理期状态",
        "markets": ["SH"], "coverage_scope": "all_mainboard_a_shares",
        "supports_historical_dates": True, "supports_complete_lists": True,
        "approved_for_local_use": True, "usage_limits": "OFFLINE_TEST 仅测试",
        "reviewed_by": "OFFLINE_TEST", "reviewed_at": "2026-09-09T09:00:00+08:00",
        "review_basis": "OFFLINE_TEST 合成完整契约",
    }


def record(value=False):
    return {
        "evidence_id": "offline-state", "source_id": "offline-source", "kind": "security_state",
        "market": "SH", "source_url": "https://example.invalid/offline/state",
        "raw_locator": "OFFLINE_TEST sentence 1", "raw_sha256": "a" * 64,
        "content_version": "offline-v1", "first_seen_at": "2026-09-09T09:01:00+08:00",
        "fetched_at": "2026-09-09T09:01:00+08:00", "reviewed_at": "2026-09-09T09:02:00+08:00",
        "effective_from": "2026-09-01", "effective_to": "2026-09-10",
        "temporal_basis": "explicit_historical_interval", "retrieval_status": "ok", "parse_status": "ok",
        "reviewed_by": "OFFLINE_TEST", "review_basis": "OFFLINE_TEST 明确区间声明",
        "evidence_excerpt": "OFFLINE_TEST 合成状态，不是任何真实公司的事实",
        "assertion_basis": "explicit_statement", "symbol": SYMBOL, "delisting_period": value,
    }


def listing(members=None):
    value = record(None)
    value.update({"evidence_id": "offline-list", "kind": "complete_delisting_list", "symbol": None,
                  "assertion_basis": "complete_list", "complete_scope": "all_mainboard_a_shares",
                  "total_pages": 1, "expected_total_records": len(members or []),
                  "retrieval_status": "ok" if members else "empty_confirmed",
                  "pages": [{"number": 1, "source_url": "https://example.invalid/offline/page1",
                             "raw_locator": "OFFLINE_TEST page 1", "raw_sha256": "b" * 64,
                             "status": "ok", "symbols": members or []}]})
    return value


def bundle(*records, sources=None):
    return {"schema_version": "eligibility-evidence-v1", "verification_kind": "offline_test",
            "sources": sources if sources is not None else [source()], "records": list(records)}


def resolve(payload):
    return resolve_eligibility(payload, [SYMBOL], T)[SYMBOL]


@pytest.mark.parametrize("value,status", [(True, "true"), (False, "false"), (None, "unknown")])
def test_three_states_preserve_values(value, status):
    result = resolve(bundle(record(value)))
    assert result["status"] == status
    assert result["delisting_period"] is value
    assert bool(result["evidence_id"]) is (value is not None)


def test_no_import_is_unknown_not_a_whitelist():
    result = resolve(load_evidence_bundle(None))
    assert result["status"] == "unknown"
    assert result["evidence"] == []


def test_history_acquired_later_is_explicit_and_preserved():
    result = resolve(bundle(record(False)))
    assert result["historical_reconstruction"] is True
    assert result["evidence"][0]["record"]["first_seen_at"] == "2026-09-09T09:01:00+08:00"
    assert result["effective_date"] == "2026-09-08"


@pytest.mark.parametrize("status", ["empty", "request_failed", "timeout", "permission_denied", "rate_limited", "partial", "schema_changed"])
def test_source_errors_never_mean_empty_negative_list(status):
    item = listing()
    item["retrieval_status"] = status
    result = resolve(bundle(item))
    assert result["status"] == "unknown"
    assert result["rejected_evidence"]


@pytest.mark.parametrize("changes", [
    {"parse_status": "failed"}, {"parse_status": "not_attempted"},
    {"effective_to": "2026-09-07"}, {"effective_from": "2026-09-09"},
    {"market": "SZ"}, {"temporal_basis": "unknown"},
    {"source_url": "https://unreviewed.invalid/state"},
    {"assertion_basis": "unknown"}, {"delisting_period": "false"},
])
def test_invalid_or_inapplicable_individual_assertions_unknown(changes):
    item = record(False)
    item.update(changes)
    result = resolve(bundle(item))
    assert result["status"] == "unknown"
    assert result["rejected_evidence"]


@pytest.mark.parametrize("changes", [
    {"supports_historical_dates": None}, {"supports_historical_dates": False},
    {"approved_for_local_use": False}, {"markets": ["SZ"]},
])
def test_unverified_source_properties_never_pass(changes):
    item = source()
    item.update(changes)
    assert resolve(bundle(record(), sources=[item]))["status"] == "unknown"


def test_current_snapshot_does_not_fill_past_date():
    item = listing()
    item.update(temporal_basis="same_date_complete_snapshot", effective_from="2026-09-08", effective_to="2026-09-08")
    assert resolve(bundle(item))["status"] == "unknown"


def test_complete_dated_list_has_positive_and_absence_negative():
    item = listing([SYMBOL, "sh.600002"])
    values = resolve_eligibility(bundle(item), [SYMBOL, "sh.600003"], T)
    assert values[SYMBOL]["delisting_period"] is True
    assert values["sh.600003"]["delisting_period"] is False


def test_explicitly_confirmed_complete_zero_list_is_negative():
    assert resolve(bundle(listing()))["delisting_period"] is False


@pytest.mark.parametrize("changes", [
    {"pages": []}, {"total_pages": 2}, {"expected_total_records": None},
    {"expected_total_records": 2}, {"complete_scope": "unknown"},
    {"assertion_basis": "unknown"},
])
def test_incomplete_lists_do_not_assert_positive_or_negative(changes):
    item = listing([SYMBOL])
    item.update(changes)
    assert resolve(bundle(item))["status"] == "unknown"


@pytest.mark.parametrize("page_changes", [
    {"number": 2}, {"status": "partial"}, {"status": "failed"},
    {"symbols": [SYMBOL, SYMBOL]}, {"symbols": ["sz.000001"]},
    {"symbols": ["bad-code"]}, {"raw_sha256": ""},
])
def test_page_parse_count_hash_and_market_checks(page_changes):
    item = listing([SYMBOL])
    item["pages"][0].update(page_changes)
    assert resolve(bundle(item))["status"] == "unknown"


def test_success_with_zero_rows_is_not_confirmed_empty():
    item = listing()
    item["retrieval_status"] = "ok"
    assert resolve(bundle(item))["status"] == "unknown"


def test_incomplete_source_scope_disallows_absence():
    item = source()
    item["coverage_scope"] = "individual_securities"
    assert resolve(bundle(listing(), sources=[item]))["status"] == "unknown"


def test_conflicts_are_not_resolved_by_order_or_known_name():
    yes, no = record(True), record(False)
    no["evidence_id"] = "offline-second-source"
    first, reversed_result = resolve(bundle(yes, no)), resolve(bundle(no, yes))
    assert first == reversed_result
    assert first["status"] == "unknown"
    assert "冲突" in first["reason"]
    assert len(first["evidence"]) == 2


@pytest.mark.parametrize("key", ["effective_from", "effective_to", "raw_sha256", "review_basis", "first_seen_at", "content_version"])
def test_missing_essential_record_field_retained_as_rejected(key):
    item = record()
    del item[key]
    result = resolve(bundle(item))
    assert result["status"] == "unknown"
    assert result["rejected_evidence"][0]["evidence"] == item


def test_one_broken_record_does_not_discard_another_security():
    good, bad = record(False), record(False)
    good["symbol"] = "sh.600002"
    good["evidence_id"] = "offline-good"
    bad.pop("effective_to")
    results = resolve_eligibility(bundle(good, bad), [SYMBOL, "sh.600002"], T)
    assert results[SYMBOL]["status"] == "unknown"
    assert results["sh.600002"]["status"] == "false"


@pytest.mark.parametrize("key", ["first_seen_at", "fetched_at", "reviewed_at"])
def test_naive_timestamps_rejected(key):
    item = record(False)
    item[key] = "2026-09-09T09:01:00"
    assert resolve(bundle(item))["status"] == "unknown"


def test_duplicate_ids_and_malformed_ids_rejected_without_crashing():
    assert resolve(bundle(record(), record()))["status"] == "unknown"
    assert resolve(bundle(record(), sources=[source(), source()]))["status"] == "unknown"
    item = record()
    item["evidence_id"] = []
    assert resolve(bundle(item))["status"] == "unknown"
    src = source()
    src["source_id"] = []
    assert resolve(bundle(record(), sources=[src]))["status"] == "unknown"


def test_import_hash_and_resolution_are_deterministic_without_now(tmp_path):
    payload = bundle(record(False))
    path = tmp_path / "offline-evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    first = load_evidence_bundle(path)
    second = load_evidence_bundle(path)
    assert first == second
    assert first["content_hash"] == "7a063bd7c0d3c9cb139c1c92448bb1ec22d769cdffb46c260b6b632ae76443eb"
    assert resolve(first) == resolve(second)
    changed = deepcopy(payload)
    changed["records"][0]["content_version"] = "offline-v2"
    path.write_text(json.dumps(changed), encoding="utf-8")
    assert load_evidence_bundle(path)["content_hash"] != first["content_hash"]


def test_missing_file_and_invalid_envelope_are_input_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_evidence_bundle(tmp_path / "missing.json")
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        load_evidence_bundle(path)
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_evidence_bundle(path)
