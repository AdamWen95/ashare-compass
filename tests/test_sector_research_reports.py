"""Offline report boundaries plus explicitly read-only archived integration checks.

Fixtures stay in memory. The two archive checks read already collected evidence;
they are not online source acceptance and never publish fixtures into research.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import socket
import sqlite3
import subprocess

import pytest

from ashare_daily.reports.m3_render import render_sections_html, render_sections_markdown
from ashare_daily.reports.sector_contracts import validate_report, validate_report_inputs
from ashare_daily.reports.sector_research import build_report, expected_history
from ashare_daily.sector_selection import digest
from test_sector_selection import calculate, inputs
from test_sector_screening import fixture, evaluate, freeze_selection


ROOT = Path(__file__).resolve().parents[1]
GENERATED = "2026-09-14T12:00:00+08:00"
REPORT_ID = "sector-report-" + "a" * 24
ENGINE_ARCHIVE = ROOT / ("outputs/engineering_validation/f3s/"
    "validation-sector-2026-09-11-e38dc2005726a1243fb1/f4s1/reports/"
    "sector-report-771cbdc01a139abcbdf80187")
PRODUCTION_ARCHIVE = ROOT / ("outputs/research/sse_szse_a/sector_reports/"
    "sector-2026-09-11-35f2b10ad94e0d109918/sector-report-ceb3d872448b62b6c0c831e8")


def seal_report(report):
    report["content_hash"] = digest({k: v for k, v in report.items() if k != "content_hash"})


def zero_selection():
    data = inputs(count=11, industries=4)
    for quote in data[3]["rows"]:
        quote.update(close=9.9, change_pct=-1)
    selected, _ = calculate(data)
    return selected


def engineering(count=2):
    selected, packets = fixture(count=count, purpose="engineering_validation")
    selected.update(taxonomy="exchange_industry_section_v1", catalog_count=19,
                    universe_count=count, parameters={})
    for index, member in enumerate(selected["members"]):
        member["name"] = "OFFLINE_TEST_" + str(index)
    freeze_selection(selected)
    technical = evaluate(selected, packets)
    qualification, readiness = [], []
    for row in technical["evaluations"]:
        row.update(cache_target_complete=True, strategy_inputs_ready=True)
        qualification.append({"security_id": row["security_id"], "eligibility_status": "pending",
            "conditions": deepcopy(row["eligibility_conditions"]),
            "gaps": [{"field": reason.split(":")[0], "reason": reason} for reason in row["risk_gaps"]],
            "material_queue_status": "diagnostic_pending", "exclusion_reasons": [],
            "facts": [{"field": field, "value": None, "target_date": selected["target_date"],
                "source_business_date": None, "observed_at": None, "fetched_at": None,
                "evidence_id": None, "source_id": None, "reason": "OFFLINE_TEST unobserved"}
                for field in ("st", "suspended", "delisting_period")]})
        readiness.append({"security_id": row["security_id"], "symbol": row["symbol"],
            "cache_status": {"cache_expected_dates": 120, "cache_raw_dates": 120,
                "cache_adjusted_dates": 120, "missing_raw_dates": [], "missing_adjusted_dates": []},
            "historical_non_trading_dates": [], "cache_target_complete": True,
            "history_window_accounted_for": True, "strategy_inputs_ready": True, "valid_history_count": 120})
    bundle = {"technical": technical, "eligibility": {"evaluations": qualification},
              "materials": {"packages": [], "status": "not_triggered"}, "readiness": readiness}
    return selected, bundle


def test_production_zero_preserves_all_sector_denominators_and_reasons():
    selection = zero_selection()
    report = build_report(selection, {}, GENERATED, REPORT_ID)
    assert report["mode"] == "offline_test"
    assert report["counts"]["stock_count"] == report["counts"]["selected_count"] == 0
    assert len(report["sector_comparison"]) == selection["catalog_count"] == 4
    assert {r["sector_id"] for r in report["sector_comparison"]} == {r["sector_id"] for r in selection["sectors"]}
    assert all(r["expected_quote_count"] == r["valid_quote_count"] == 11 for r in report["sector_comparison"])
    assert all(r["reasons"] == ["nonpositive_daily_change_or_amount"] and r["reason_texts"] for r in report["sector_comparison"])
    assert report["history_status"] == "not_triggered" and report["history_coverage_ratio"] is None
    assert report["eligibility_coverage_ratio"] is None and report["company_materials_status"] == "not_triggered"
    assert "整个市场没有机会" not in report["conclusion"]


def test_production_zero_never_imports_engineering_queue_from_other_bundle():
    _, foreign = engineering(1)
    report = build_report(zero_selection(), foreign, GENERATED, REPORT_ID)
    assert report["evaluations"] == report["materials"] == report["history_gap_accounting"] == []
    assert report["counts"]["formal_candidate_count"] == report["counts"]["model_queue_count"] == 0
    assert "validation-sector-" not in json.dumps(report, ensure_ascii=False)


def test_nonempty_production_is_explicitly_rejected():
    selected, _ = calculate(inputs(1))
    with pytest.raises(ValueError, match="production_zero_report"):
        build_report(selected, {}, GENERATED, REPORT_ID)


@pytest.mark.parametrize("field", ["selection_verified", "industry_comparison_complete"])
def test_failed_source_cannot_be_reported_as_zero_opportunity(field):
    selected = zero_selection()
    selected[field] = False
    freeze_selection(selected)
    with pytest.raises(ValueError, match="production_zero_report"):
        build_report(selected, {}, GENERATED, REPORT_ID)


@pytest.mark.parametrize("count", [1, 103])
def test_engineering_all_members_kept_without_seven_or_hundred_cap(count):
    selected, bundle = engineering(count)
    report = build_report(selected, bundle, GENERATED, REPORT_ID)
    assert report["counts"]["stock_count"] == len(report["evaluations"]) == count
    assert {r["security_id"] for r in report["evaluations"]} == {m["security_id"] for m in selected["members"]}
    assert report["production_eligible"] is False and report["report_kind"] == "engineering_diagnostic"
    assert all(r["formal_candidate"] is False and r["model_queue_eligible"] is False for r in report["evaluations"])


def test_unknown_risk_and_unknown_source_business_dates_are_preserved():
    selected, bundle = engineering(1)
    report = build_report(selected, bundle, GENERATED, REPORT_ID)
    row = report["evaluations"][0]
    assert row["risk_states"] == {"st": None, "suspended": None, "delisting_period": None}
    assert row["eligibility_status"] == "pending"
    assert all(f["value"] is None and f["source_business_date"] is None for f in row["eligibility_facts"])
    assert any(c["status"] == "unknown" for c in row["eligibility_conditions"])
    text = render_sections_markdown(report["title"], report["notice"], report["sections"])
    assert "unknown" in text and "null" in text


def test_explained_315_rows_do_not_upgrade_320_cache_or_119_of_120_strategy():
    selected, bundle = engineering(1)
    missing = ["2025-06-10", "2025-11-13", "2025-11-14", "2025-11-17", "2026-04-22"]
    record = bundle["readiness"][0]
    record.update(cache_target_complete=False, strategy_inputs_ready=False, valid_history_count=119,
                  historical_non_trading_dates=missing)
    record["cache_status"].update(cache_expected_dates=320, cache_raw_dates=315, cache_adjusted_dates=315,
                                  missing_raw_dates=missing, missing_adjusted_dates=missing)
    original = deepcopy(record)
    actual = expected_history([record])[0]
    assert record == original
    assert actual["expected_required_rows"] == actual["actual_raw_rows"] == 315
    assert actual["cache_target_rows"] == 320 and actual["cache_target_complete"] is False
    assert actual["fixed_strategy_valid_days"] == 119 and actual["strategy_inputs_ready"] is False
    assert actual["price_rows_added"] == actual["calendar_dates_skipped"] == 0


def test_non_trading_explanation_must_belong_to_both_missing_windows():
    _, bundle = engineering(1)
    bundle["readiness"][0]["historical_non_trading_dates"] = ["2026-04-22"]
    with pytest.raises(ValueError, match="not_bound_to_missing_dates"):
        expected_history(bundle["readiness"])


def test_sorted_json_roundtrip_and_rebuild_are_deterministic_without_mutation():
    selected, bundle = engineering(2)
    before = deepcopy((selected, bundle))
    report = build_report(selected, bundle, GENERATED, REPORT_ID)
    restored = json.loads(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    rebuilt = build_report(json.loads(json.dumps(selected, sort_keys=True)),
                           json.loads(json.dumps(bundle, sort_keys=True)), GENERATED, REPORT_ID)
    assert report == restored == rebuilt
    assert validate_report(restored, selected) == report and before == (selected, bundle)
    frozen = {"selection_content_hash": selected["content_hash"], "purpose": report["purpose"],
              "actual_generated_at": GENERATED, "bundle": bundle}
    assert validate_report_inputs(restored, selected, frozen) == restored
    for field in ("per_member_expected", "cache_complete_count", "strategy_ready_count", "actual_raw_rows"):
        # On disk each duplicate representation is independent; deepcopy of
        # the fresh projection retains aliases and would conceal this defect.
        changed = json.loads(json.dumps(report, ensure_ascii=False, sort_keys=True))
        if field == "per_member_expected":
            changed["evaluations"][0]["expected_history"]["expected_required_rows"] = 1
        else:
            changed["counts"][field] += 1
        seal_report(changed)
        with pytest.raises(ValueError):
            validate_report_inputs(changed, selected, frozen)


def test_html_escapes_untrusted_text_and_blocks_scripts_with_csp():
    selected, bundle = engineering(1)
    malicious = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    selected["members"][0]["name"] = malicious
    freeze_selection(selected)
    bundle["technical"]["evaluations"][0]["name"] = malicious
    report = build_report(selected, bundle, GENERATED, REPORT_ID)
    rendered = render_sections_html(report["title"], report["trade_date"], report["notice"], report["sections"])
    assert "&lt;script&gt;" in rendered and malicious not in rendered
    class Inspector(HTMLParser):
        unsafe = []
        policies = []
        def handle_starttag(self, tag, attrs):
            properties = dict(attrs)
            if tag == "meta" and properties.get("http-equiv", "").casefold() == "content-security-policy":
                self.policies.append(properties["content"])
            if tag in {"script", "iframe", "img"} or any(k.startswith("on") for k, _ in attrs):
                self.unsafe.append(tag)
    parser = Inspector()
    parser.feed(rendered)
    assert parser.unsafe == []
    assert len(parser.policies) == 1
    directives = {part.strip().split()[0]: part.strip().split()[1:]
                  for part in parser.policies[0].split(";") if part.strip()}
    # A missing script-src inherits default-src; both valid spellings must
    # deny every script, rather than demanding a particular serialization.
    assert directives.get("script-src", directives.get("default-src")) == ["'none'"]
    assert directives.get("form-action") == ["'none'"]


def test_pure_reporting_never_needs_network_database_or_model_process(monkeypatch):
    selected, bundle = engineering(1)
    def forbidden(*args, **kwargs):
        raise AssertionError("pure report attempted external execution")
    for owner, attribute in ((socket, "socket"), (sqlite3, "connect"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, attribute, forbidden)
    report = build_report(selected, bundle, GENERATED, REPORT_ID)
    for key in ("model_calls", "model_tokens", "network_requests", "database_writes"):
        assert report[key] == 0
    assert report["f4s2_ready"] is False


@pytest.mark.parametrize("change", ["remove", "duplicate"])
def test_report_member_truncation_or_replacement_rejected_even_after_rehash(change):
    selected, bundle = engineering(2)
    report = build_report(selected, bundle, GENERATED, REPORT_ID)
    if change == "remove":
        report["evaluations"].pop()
    else:
        report["evaluations"][1] = deepcopy(report["evaluations"][0])
    seal_report(report)
    with pytest.raises(ValueError, match="members_changed_or_truncated"):
        validate_report(report, selected)


def test_nonzero_calls_and_invalid_observation_times_are_rejected_after_rehash():
    selected = zero_selection()
    original = build_report(selected, {}, GENERATED, REPORT_ID)
    changes = [{field: 1} for field in ("network_requests", "model_calls", "model_tokens", "database_writes")]
    changes += [{"actual_generated_at": "2026-09-14T12:00:00"},
                {"actual_generated_at": "2026-09-10T12:00:00+08:00"},
                {"actual_generated_at": (datetime.now().astimezone()+timedelta(days=1)).isoformat()}]
    for change in changes:
        report = deepcopy(original)
        report.update(change)
        seal_report(report)
        with pytest.raises(ValueError):
            validate_report(report, selected)


def test_report_cannot_create_candidate_or_convert_engineering_purpose():
    selected, bundle = engineering(1)
    original = build_report(selected, bundle, GENERATED, REPORT_ID)
    for change in ("formal_candidate_count", "model_queue_count", "purpose", "f4s2_ready"):
        report = deepcopy(original)
        if change in report["counts"]:
            report["counts"][change] = 1
        else:
            report[change] = "production" if change == "purpose" else True
        seal_report(report)
        with pytest.raises(ValueError):
            validate_report(report, selected)


def test_duplicate_industry_cannot_replace_missing_industry_in_zero_report():
    selected = zero_selection()
    report = build_report(selected, {}, GENERATED, REPORT_ID)
    report["sector_comparison"][1] = deepcopy(report["sector_comparison"][0])
    seal_report(report)
    with pytest.raises(ValueError):
        validate_report(report, selected)


def archive(directory):
    if not directory.is_dir():
        pytest.skip("real archive absent; offline unit tests above remain runnable")
    manifest = json.loads((directory/"manifest.json").read_text(encoding="utf-8"))
    before = {}
    for name, value in manifest["files"].items():
        before[name] = hashlib.sha256((directory/name).read_bytes()).hexdigest()
        assert before[name] == value
    selected = json.loads((directory/"sector_selection.json").read_text(encoding="utf-8"))
    report = json.loads((directory/"sector_research_report.json").read_text(encoding="utf-8"))
    validate_report(report, selected)
    assert all(hashlib.sha256((directory/name).read_bytes()).hexdigest() == value for name, value in before.items())
    return report, selected


def test_local_archive_integration_production_zero_is_isolated_and_complete():
    """Read-only saved real report; no online acceptance is performed."""
    report, selected = archive(PRODUCTION_ARCHIVE)
    assert report["mode"] == "research" and report["purpose"] == "production"
    assert len(report["sector_comparison"]) == selected["catalog_count"] == 19
    assert report["counts"]["stock_count"] == 0 and report["materials"] == []
    assert sum(r["scope_member_count"] for r in report["sector_comparison"]) == 5218
    assert sum(r["valid_quote_count"] for r in report["sector_comparison"]) == 5213
    assert sum(len(r["full_day_halted"]) for r in report["sector_comparison"]) == 5


def test_local_archive_integration_engineering_retains_material_and_gap_limits():
    """Read-only saved real report; specific observed numbers are not fixtures."""
    report, selected = archive(ENGINE_ARCHIVE)
    assert report["production_eligible"] is False and len(report["evaluations"]) == len(selected["members"]) == 7
    by_symbol = {r["symbol"]: r for r in report["evaluations"]}
    assert by_symbol["sz.000609"]["expected_history"]["expected_required_rows"] == 315
    assert by_symbol["sz.000609"]["expected_history"]["fixed_strategy_valid_days"] == 119
    assert by_symbol["sh.600673"]["expected_history"]["expected_required_rows"] == 311
    assert by_symbol["sh.600673"]["technical_status"] == "fail"
    assert report["company_materials_status"] == "blocked" and len(report["materials"]) == 1
    material = report["materials"][0]
    assert material["symbol"] == "sz.000532" and material["search_record"]["match_count"] == 0
    assert material["main_business"] is None and material["benefit_evidence"] is None
    assert material["documents"] == material["facts"] == material["counterevidence"] == []
    assert report["counts"]["formal_candidate_count"] == report["counts"]["model_queue_count"] == 0
