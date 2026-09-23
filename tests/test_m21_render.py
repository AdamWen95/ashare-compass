"""人工离线模板输入，不构成真实行情或资格来源证据。"""

from copy import deepcopy
import csv
from html.parser import HTMLParser
import json

import pytest

from ashare_daily.reports.m21_render import (
    NOTICE, m21_sections, render_m21_html, render_m21_markdown, write_m21_audit_csv,
)


def _recount(report):
    rows = report["evaluations"]
    report["candidates"] = [row for row in rows if row["status"] == "candidate"]
    report["pending_eligibility"] = sorted((row for row in rows if row["technical_screen_status"] == "pass" and row["eligibility_status"] == "pending"), key=lambda row: row["symbol"])
    counts = {"stock_count": len(rows), "configured_stock_count": len(rows), "market_data_success_count": len(rows),
              "candidate_count": len(report["candidates"]), "pending_eligibility_count": len(report["pending_eligibility"]),
              "stocks_with_data_gaps": sum(bool(row["data_issues"]) for row in rows),
              "eligibility_verified_count": sum(all(item["status"] != "unknown" for item in row["eligibility_conditions"]) for row in rows),
              "eligibility_conclusion_count": sum(row["eligibility_status"] != "pending" for row in rows)}
    for status in ("excluded", "data_insufficient", "qualified_not_selected"):
        counts[f"{status}_count"] = sum(row["status"] == status for row in rows)
    for status in ("pass", "fail", "not_computable"):
        counts[f"technical_{status}_count"] = sum(row["technical_screen_status"] == status for row in rows)
    for status in ("pass", "fail", "pending"):
        counts[f"eligibility_{status}_count"] = sum(row["eligibility_status"] == status for row in rows)
    report["counts"] = counts
    return report


@pytest.fixture
def split_report():
    technical = [{"id": "trend", "label": "趋势", "status": "pass", "reason": "人工 11 > 10 > 9"}]
    eligibility = [{"id": "not_delisting_period", "label": "非退市整理期", "status": "pass", "reason": "人工完整名单，仅用于离线测试"}]
    row = {"symbol": "sh.600000", "name": "人工正式甲", "analysis_date": "2026-09-08", "actual_data_date": "2026-09-08",
           "security_type": "stock", "valid_history_count": 120, "display_close": "12.34567890123456789", "display_daily_return": "0.01",
           "price_unit": "CNY", "display_adjustment_mode": "unadjusted", "trend_adjustment_mode": "forward_adjusted",
           "adjusted_close": "11", "ma_short": "10", "ma_long": "9", "period_return": "0.1", "benchmark_period_return": "0.01",
           "relative_return": "0.09", "avg_amount_cny": "50000000.0001", "status": "candidate", "rank": 1,
           "technical_screen_status": "pass", "eligibility_status": "pass", "delisting_period_status": "false",
           "technical_conditions": technical, "eligibility_conditions": eligibility, "conditions": technical + eligibility,
           "eligibility_evidence": [{"source": "人工测试来源", "source_locator": "https://example.invalid/offline-only",
                                    "fetched_at": "2026-09-09T12:00:00+08:00", "effective_date": "2026-09-08", "content_version": "test-hash"}],
           "exclusion_reasons": [], "data_issues": [], "selection_reasons": ["人工量价条件与资格通过"]}
    pending = deepcopy(row)
    pending.update(symbol="sz.000001", name="人工待查乙", status="data_insufficient", rank=None, eligibility_status="pending",
                   delisting_period_status="unknown", eligibility_evidence=[], selection_reasons=[], data_issues=["目标日期退市整理证据缺失"])
    pending["eligibility_conditions"][0].update(status="unknown", reason="目标日期退市整理证据缺失")
    pending["conditions"] = pending["technical_conditions"] + pending["eligibility_conditions"]
    failed = deepcopy(pending)
    failed.update(symbol="sh.600036", name="人工失败丙", status="excluded", technical_screen_status="fail", relative_return="-0.02",
                  exclusion_reasons=["量价趋势条件未通过"])
    failed["technical_conditions"][0].update(status="fail", reason="量价趋势条件未通过")
    failed["conditions"] = failed["technical_conditions"] + failed["eligibility_conditions"]
    report = {"title": "今日方向简报 · M2.1 人工测试", "notice": NOTICE, "mode": "research", "verification_kind": "offline_test",
              "workflow_version": "m2.1", "status_semantics_version": "eligibility-split-v1", "news_review_status": "未完成",
              "trade_date": "2026-09-08", "actual_market_date": "2026-09-08", "actual_generated_at": "2026-09-09T12:00:00+08:00",
              "timezone": "Asia/Shanghai", "status": "partial", "scope": "人工三个股票样本；非全市场", "snapshot_id": "test-snapshot-123",
              "config_hash": "test-config-456", "strategy_version": "trend_research_v1.0.0", "result_hash": "test-result-789",
              "strategy_config": {"min_history_trading_days": 120, "ma_short_days": 20, "ma_long_days": 60, "return_days": 20,
                                  "amount_days": 20, "min_avg_amount_cny": "50000000", "max_candidates": 20},
              "metric_windows": {"ma_short": 20, "ma_long": 60, "period_return": 20, "avg_amount_cny": 20},
              "benchmark": {"symbol": "sh.000001", "name": "人工指数", "actual_data_date": "2026-09-08", "display_close": "3000",
                            "display_preclose": "2990", "daily_return": "0.003", "period_return": "0.01", "price_unit": "index_points",
                            "display_adjustment_mode": "unadjusted", "trend_adjustment_mode": "index_native", "issues": []},
              "evaluations": [row, pending, failed], "non_stock_records": [{"symbol": "sh.000001", "name": "人工指数", "reason": "仅作基准"}],
              "gaps": ["两个人工股票缺退市整理状态证据"], "boundaries": ["仅离线人工测试，无真实资格证据"]}
    return _recount(report)


@pytest.mark.parametrize("renderer", [render_m21_html, render_m21_markdown])
def test_split_states_scope_dates_units_and_count_semantics(split_report, renderer):
    text = renderer(split_report)
    for token in (NOTICE, "量价通过", "量价不通过", "资格通过", "资格待核实", "重叠计数", "主分类互斥", "未计入正式 candidate", "未完成",
                  "2026-09-08", "2026-09-09T12:00:00+08:00", "Asia/Shanghai", "50,000,000.00 元", "OFFLINE TEST", "test-snapshot-123"):
        assert token.replace("_", "\\_") in text if renderer is render_m21_markdown else token in text
    assert len(m21_sections(split_report)) == 8


def test_pending_table_has_no_rank_and_does_not_enter_formal_section(split_report):
    sections = m21_sections(split_report)
    formal = str(sections[4])
    pending = str(sections[5])
    assert "人工正式甲" in formal and "人工待查乙" not in formal and "人工失败丙" not in formal
    assert "人工待查乙" in pending and "人工正式甲" not in pending and "人工失败丙" not in pending
    tables = [block[1] for block in sections[5][1] if block[0] == "table"]
    assert all("rank" not in header and "排名" not in header for header in tables[0][0])
    assert split_report["counts"]["candidate_count"] == 1
    assert split_report["counts"]["pending_eligibility_count"] == 1


@pytest.mark.parametrize("renderer", [render_m21_html, render_m21_markdown])
def test_zero_candidate_and_fail_with_unknown_preserve_both_dimensions(split_report, renderer):
    split_report["evaluations"] = split_report["evaluations"][1:]
    _recount(split_report)
    text = renderer(split_report)
    assert "本期正式量价预候选为 0" in text
    assert "量价趋势条件未通过" in text and "目标日期退市整理证据缺失" in text
    assert split_report["counts"]["excluded_count"] == 1
    assert split_report["counts"]["data_insufficient_count"] == 1
    assert split_report["counts"]["stocks_with_data_gaps"] == 2


def test_all_stock_csv_has_split_states_precise_values_evidence_and_bom(split_report, tmp_path):
    path = tmp_path / "audit.csv"
    assert write_m21_audit_csv(split_report, path) == 3
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["display_close"] == "12.34567890123456789"
    assert rows[0]["avg_amount_cny"] == "50000000.0001"
    assert rows[0]["delisting_period_status"] == "false"
    assert rows[1]["technical_screen_status"] == "pass" and rows[1]["eligibility_status"] == "pending"
    assert rows[1]["rank"] == "" and rows[1]["condition_not_delisting_period_status"] == "unknown"
    assert rows[2]["technical_screen_status"] == "fail" and rows[2]["eligibility_status"] == "pending"
    assert rows[2]["relative_return"] == "-0.02"
    assert json.loads(rows[0]["eligibility_evidence"])[0]["fetched_at"] == "2026-09-09T12:00:00+08:00"
    assert rows[0]["news_review_status"] == "未完成"
    assert rows[0]["workflow_version"] == "m2.1"


@pytest.mark.parametrize("mutation", ["candidate_unknown", "pending_rank", "pending_missing", "pending_invented", "count", "news", "workflow", "delisting", "duplicate"])
def test_refuse_misleading_or_inconsistent_publication(split_report, mutation):
    if mutation == "candidate_unknown":
        split_report["evaluations"][0]["eligibility_status"] = "pending"
    elif mutation == "pending_rank":
        split_report["pending_eligibility"][0]["rank"] = 2
    elif mutation == "pending_missing":
        split_report["pending_eligibility"] = []
    elif mutation == "pending_invented":
        split_report["pending_eligibility"].append(split_report["evaluations"][2])
    elif mutation == "count":
        split_report["counts"]["stocks_with_data_gaps"] = 0
    elif mutation == "news":
        split_report["news_review_status"] = "已完成"
    elif mutation == "workflow":
        split_report["workflow_version"] = "m2"
    elif mutation == "delisting":
        split_report["evaluations"][0]["delisting_period_status"] = None
    elif mutation == "duplicate":
        split_report["evaluations"].append(split_report["evaluations"][0])
    with pytest.raises(ValueError):
        render_m21_html(split_report)


def test_source_text_html_markdown_and_csv_are_escaped(split_report, tmp_path):
    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []

        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)

    split_report["evaluations"][0]["name"] = "=HYPERLINK(1)"
    split_report["evaluations"][0]["eligibility_evidence"][0]["source"] = "<script>run()</script>{{body}} [bad](https://bad.invalid)"
    html = render_m21_html(split_report)
    tags = Tags()
    tags.feed(html)
    assert "script" not in tags.tags
    assert "&lt;script&gt;run()&lt;/script&gt;{{body}}" in html
    assert "Content-Security-Policy" in html and tags.tags.count("section") == 8
    md = render_m21_markdown(split_report)
    assert "<script>" not in md and "\\[bad\\]\\(https://bad.invalid\\)" in md
    path = tmp_path / "audit.csv"
    write_m21_audit_csv(split_report, path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        assert next(csv.DictReader(handle))["name"] == "'=HYPERLINK(1)"


def test_rendering_does_not_mutate_frozen_report_and_is_deterministic(split_report, tmp_path):
    before = deepcopy(split_report)
    first = render_m21_html(split_report), render_m21_markdown(split_report)
    path = tmp_path / "audit.csv"
    write_m21_audit_csv(split_report, path)
    csv_bytes = path.read_bytes()
    assert (render_m21_html(split_report), render_m21_markdown(split_report)) == first
    write_m21_audit_csv(split_report, path)
    assert path.read_bytes() == csv_bytes
    assert split_report == before


def test_unknown_metrics_empty_csv_and_unknown_not_zero(split_report, tmp_path):
    row = split_report["evaluations"][1]
    row.update(technical_screen_status="not_computable", ma_short=None, ma_long=None, relative_return=None)
    row["technical_conditions"][0].update(status="unknown", reason="历史不足")
    _recount(split_report)
    text = render_m21_html(split_report)
    assert "量价无法计算" in text and "无法判断（数据不足）" in text
    path = tmp_path / "audit.csv"
    write_m21_audit_csv(split_report, path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[1]["ma_short"] == "" and rows[1]["relative_return"] == ""
    assert split_report["counts"]["pending_eligibility_count"] == 0


@pytest.mark.parametrize("renderer", [render_m21_html, render_m21_markdown])
def test_delisting_true_is_explicit_failure_and_never_formal(split_report, renderer):
    row = split_report["evaluations"][0]
    row.update(eligibility_status="fail", delisting_period_status="true", status="excluded", rank=None,
               exclusion_reasons=["人工证据确认处于退市整理期"])
    row["eligibility_conditions"][0].update(status="fail", reason="人工证据确认处于退市整理期")
    _recount(split_report)
    text = renderer(split_report)
    assert "确认处于退市整理期" in text and "资格不通过" in text
    assert split_report["counts"]["candidate_count"] == 0


def test_known_eligibility_failure_with_other_unknown_is_not_full_coverage(split_report):
    row = split_report["evaluations"][1]
    row.update(eligibility_status="fail", status="excluded", exclusion_reasons=["人工样本为 ST"])
    row["eligibility_conditions"].append({"id": "not_st", "label": "非 ST", "status": "fail", "reason": "人工样本为 ST"})
    row["conditions"] = row["technical_conditions"] + row["eligibility_conditions"]
    _recount(split_report)
    assert split_report["counts"]["eligibility_conclusion_count"] == 2
    assert split_report["counts"]["eligibility_verified_count"] == 1
    assert split_report["counts"]["stocks_with_data_gaps"] == 2
    text = render_m21_html(split_report)
    assert "资格核验覆盖数（所有必要资格字段均已判定）" in text
    assert "人工样本为 ST" in text and "目标日期退市整理证据缺失" in text
