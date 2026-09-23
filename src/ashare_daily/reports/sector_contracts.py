"""Pure F4-S1 report checks shared by publisher and read-only viewer."""
from datetime import datetime
import re
from ashare_daily.artifact_purpose import is_production_artifact
from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.sector_selection import digest, verify_selection
SCHEMA = "f4s1-sector-report-v1"


def validate_report(report, selection):
    verify_selection(selection)
    engineering = selection.get("purpose") == "engineering_validation"
    purpose = "engineering_validation" if engineering else "production"
    if (report.get("schema_version") != SCHEMA or report.get("purpose") != purpose or report.get("production_eligible") is not (not engineering)
            or report.get("content_hash") != digest({key: value for key, value in report.items() if key != "content_hash"})
            or report.get("selection_id") != selection["selection_id"] or report.get("selection_content_hash") != selection["content_hash"]
            or report.get("trade_date") != selection["target_date"] or report.get("mode") != selection["mode"]
            or report.get("market_scope") != selection["market_scope"] or report.get("research_mode") != "sector_first"
            or not re.fullmatch(r"sector-report-[a-f0-9]{24}", report.get("report_id", ""))):
        raise ValueError("sector_report_scope_or_hash_mismatch")
    generated = datetime.fromisoformat(report["actual_generated_at"])
    if generated.utcoffset() is None or generated > datetime.now(SHANGHAI) or generated < datetime.fromisoformat(selection["cutoff_at"]):
        raise ValueError("sector_report_time_invalid")
    rows = report["evaluations"]
    if len(rows) != len(selection["members"]) or {r["security_id"] for r in rows} != {m["security_id"] for m in selection["members"]}:
        raise ValueError("sector_report_members_changed_or_truncated")
    counts = report["counts"]
    if counts.get("stock_count") != len(rows) or any(report.get(key) != 0 for key in ("model_calls", "model_tokens", "network_requests", "database_writes")):
        raise ValueError("sector_report_execution_boundary_violation")
    if counts.get("formal_candidate_count") != 0 or counts.get("model_queue_count") != 0 or report.get("f4s2_ready") is not False:
        raise ValueError("sector_report_may_not_create_candidates_or_models")
    for axis, states in (("technical", ("pass", "fail", "unknown", "not_applicable")), ("eligibility", ("pass", "fail", "pending"))):
        if any(row.get(axis+"_status") not in states for row in rows):
            raise ValueError("sector_report_unknown_status_contract")
        for state in states:
            if counts.get(axis+"_"+state+"_count") != sum(row[axis+"_status"] == state for row in rows):
                raise ValueError("sector_report_counts_do_not_match_members")
    if not engineering and (not is_production_artifact(report) or rows or report["materials"] or report["history_gap_accounting"]
            or selection.get("selection_status") != "no_matching_sectors" or not selection.get("industry_comparison_complete")
            or counts.get("preselected_count") != 0 or counts.get("selected_count") != 0
            or len(report["sector_comparison"]) != selection["catalog_count"]
            or report["conclusion"] != "本分类体系下无行业满足当前观察规则。"):
        raise ValueError("production_zero_report_contains_foreign_or_incomplete_scope")
    if not engineering:
        expected = {row["sector_id"]: row for row in selection["sectors"]}
        compared = report["sector_comparison"]
        if len(expected) != len(compared) or {row["sector_id"] for row in compared} != set(expected):
            raise ValueError("sector_comparison_duplicate_or_missing_industry")
        for row in compared:
            if {key: value for key, value in row.items() if key != "reason_texts"} != expected[row["sector_id"]]:
                raise ValueError("sector_comparison_changed_from_frozen_source")
    if engineering and report.get("report_kind") != "engineering_diagnostic":
        raise ValueError("engineering_report_cannot_be_production")
    return report


def validate_report_inputs(report, selection, inputs):
    """Check frozen numerical/eligibility content without rerendering old prose."""
    validate_report(report, selection)
    if (inputs.get("selection_content_hash") != selection["content_hash"] or inputs.get("purpose") != report["purpose"]
            or inputs.get("actual_generated_at") != report["actual_generated_at"]):
        raise ValueError("sector_report_frozen_input_mismatch")
    if report["purpose"] == "production":
        source = {row["sector_id"]: row for row in selection["sectors"]}
        if {row["sector_id"] for row in report["sector_comparison"]} != set(source):
            raise ValueError("sector_comparison_members_changed")
        for row in report["sector_comparison"]:
            if {key: value for key, value in row.items() if key != "reason_texts"} != source[row["sector_id"]]:
                raise ValueError("sector_comparison_changed_from_frozen_source")
        if inputs["bundle"]:
            raise ValueError("production_empty_report_contains_engineering_inputs")
        return report
    bundle = inputs["bundle"]
    technical = {row["security_id"]: row for row in bundle["technical"]["evaluations"]}
    eligibility = {row["security_id"]: row for row in bundle["eligibility"]["evaluations"]}
    if set(technical) != set(eligibility) or set(technical) != {row["security_id"] for row in report["evaluations"]}:
        raise ValueError("report_engine_members_differ")
    replaced = {"eligibility_status", "eligibility_conditions", "eligibility_gaps", "exclusion_reasons", "risk_gaps",
                "history_window_accounted_for", "production_eligible"}
    for row in report["evaluations"]:
        sid = row["security_id"]
        if any(row.get(key) != value for key, value in technical[sid].items() if key not in replaced):
            raise ValueError("report_technical_values_differ_from_engine")
        q = eligibility[sid]
        for field, key in (("eligibility_status", "eligibility_status"), ("eligibility_conditions", "conditions"),
                           ("eligibility_facts", "facts"), ("eligibility_gaps", "gaps"), ("exclusion_reasons", "exclusion_reasons")):
            if row[field] != q[key]:
                raise ValueError("report_qualification_values_differ_from_engine")
    if report["materials"] != bundle["materials"]["packages"]:
        raise ValueError("report_company_materials_differ_from_frozen_inputs")
    expected = expected_history(bundle["readiness"])
    if report["history_gap_accounting"] != expected:
        raise ValueError("report_expected_history_not_bound_to_state_evidence")
    expected_by_id = {row["security_id"]: row for row in expected}
    for row in report["evaluations"]:
        if row["expected_history"] != expected_by_id[row["security_id"]]:
            raise ValueError("report_member_history_does_not_match_global_accounting")
    derived_counts = {"expected_required_rows": sum(row["expected_required_rows"] for row in expected),
        "actual_raw_rows": sum(row["actual_raw_rows"] for row in expected),
        "cache_complete_count": sum(row["cache_target_complete"] for row in bundle["readiness"]),
        "strategy_ready_count": sum(row["strategy_inputs_ready"] for row in bundle["readiness"])}
    if any(report["counts"].get(key) != value for key, value in derived_counts.items()):
        raise ValueError("report_expected_history_count_mismatch")
    return report



def expected_history(readiness):
    """Change only the explained non-trading denominator, never prices or rules."""
    result = []
    for row in readiness:
        cache = row["cache_status"]
        dates = set(row["historical_non_trading_dates"])
        missing_raw = set(cache["missing_raw_dates"])
        missing_adjusted = set(cache["missing_adjusted_dates"])
        if not dates <= missing_raw or not dates <= missing_adjusted:
            raise ValueError("non_trading_explanation_not_bound_to_missing_dates")
        result.append({"security_id": row["security_id"], "symbol": row["symbol"],
            "cache_target_rows": cache["cache_expected_dates"], "cache_target_complete": row["cache_target_complete"],
            "evidenced_non_trading_dates": sorted(dates), "expected_required_rows": cache["cache_expected_dates"]-len(dates),
            "actual_raw_rows": cache["cache_raw_dates"], "actual_adjusted_rows": cache["cache_adjusted_dates"],
            "unexplained_raw_dates": sorted(missing_raw-dates), "unexplained_adjusted_dates": sorted(missing_adjusted-dates),
            "history_window_accounted_for": row["history_window_accounted_for"],
            "strategy_inputs_ready": row["strategy_inputs_ready"], "fixed_strategy_valid_days": row["valid_history_count"],
            "price_rows_added": 0, "calendar_dates_skipped": 0})
    return result

