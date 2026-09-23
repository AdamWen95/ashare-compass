"""M2 展示与 CSV 使用独立固定输入；这些人工数据从不充当真实接口证据。"""

from copy import deepcopy
import csv
from html.parser import HTMLParser

import pytest

from ashare_daily.reports.m2_render import NOTICE, render_m2_html, render_m2_markdown, write_audit_csv


@pytest.fixture
def m2_report():
    row = {
        "symbol": "sh.600000", "name": "人工样本甲", "analysis_date": "2026-09-08", "actual_data_date": "2026-09-08",
        "security_type": "stock", "valid_history_count": 120, "display_close": "12.34567890123456789",
        "display_daily_return": "0.01", "price_unit": "CNY", "display_adjustment_mode": "unadjusted",
        "trend_adjustment_mode": "forward_adjusted", "adjusted_close": "11", "ma_short": "10", "ma_long": "9",
        "period_return": "0.1", "benchmark_period_return": "0.01", "relative_return": "0.09", "avg_amount_cny": "50000000.0001",
        "status": "candidate", "rank": 1,
        "conditions": [{"id": "history", "label": "有效历史", "status": "pass", "reason": "120 >= 120"},
                       {"id": "trend", "label": "趋势", "status": "pass", "reason": "11 > 10 > 9"}],
        "exclusion_reasons": [], "data_issues": [], "selection_reasons": ["历史、成交额、趋势和相对收益条件通过"],
    }
    unknown = deepcopy(row)
    unknown.update(symbol="sz.000001", name="人工样本乙", status="data_insufficient", rank=None,
                   actual_data_date="2026-09-07", ma_short=None, ma_long=None, relative_return=None,
                   exclusion_reasons=["必要状态未知"], data_issues=["缺失分析日数据"], selection_reasons=[],
                   conditions=[{"id": "history", "label": "有效历史", "status": "pass", "reason": "120 >= 120"},
                               {"id": "trend", "label": "趋势", "status": "unknown", "reason": "缺失分析日数据"}])
    failed = deepcopy(row)
    failed.update(symbol="sh.600036", name="人工样本丙", status="excluded", rank=None, relative_return="-0.02",
                  exclusion_reasons=["相对收益条件未通过"], selection_reasons=[],
                  conditions=[{"id": "relative", "label": "相对收益", "status": "fail", "reason": "-0.02 <= 0"}])
    return {
        "title": "今日方向简报 · M2", "notice": NOTICE, "mode": "research", "verification_kind": "offline_test",
        "trade_date": "2026-09-08", "actual_market_date": "2026-09-08", "actual_generated_at": "2026-09-09T12:00:00+08:00",
        "timezone": "Asia/Shanghai", "status": "partial", "scope": "三个股票人工样本和一个人工指数样本",
        "snapshot_id": "snapshot-123", "config_hash": "config-456", "strategy_version": "m2-test-v1",
        "strategy_config": {"min_history_trading_days": 120, "ma_short_days": 20, "ma_long_days": 60, "return_days": 20,
                            "amount_days": 20, "min_avg_amount_cny": "50000000", "max_candidates": 20},
        "metric_windows": {"ma_short": 20, "ma_long": 60, "period_return": 20, "avg_amount_cny": 20},
        "benchmark": {"symbol": "sh.000001", "name": "人工指数", "actual_data_date": "2026-09-08", "display_close": "3000",
                      "display_preclose": "2990", "daily_return": "0.0033444816", "period_return": "0.01", "price_unit": "index_points",
                      "display_adjustment_mode": "unadjusted", "trend_adjustment_mode": "unadjusted", "issues": []},
        "counts": {"stock_count": 3, "candidate_count": 1, "excluded_count": 1, "data_insufficient_count": 1, "qualified_not_selected_count": 0},
        "candidates": [row], "evaluations": [row, unknown, failed],
        "non_stock_records": [{"symbol": "sh.000001", "name": "人工指数", "reason": "指数仅用作基准"}],
        "gaps": ["人工样本乙缺失分析日数据"], "boundaries": ["未覆盖真实外部信息"], "result_hash": "result-789",
    }


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


@pytest.mark.parametrize("renderer", [render_m2_html, render_m2_markdown])
def test_report_seven_sections_dates_states_units_and_scope(m2_report, renderer):
    text = renderer(m2_report)
    for heading in ["一、数据状态", "二、基准指数", "三、当前股票", "四、本期筛选", "五、量价预候选", "六、未通过", "七、数据缺口"]:
        assert heading in text
    for token in [NOTICE, "2026-09-08", "2026-09-07", "2026-09-09T12:00:00+08:00", "Asia/Shanghai", "50,000,000.00 元",
                  "3,000.0000 点", "9.0000%", "数据不足，无法判断", "未通过条件", "前复权", "未复权", "未覆盖", "OFFLINE TEST"]:
        assert token in text
    assert "snapshot-123" in text and "config-456" in text and "result-789" in text


@pytest.mark.parametrize("renderer", [render_m2_html, render_m2_markdown])
def test_zero_candidates_is_normal_and_preserves_unknown_reasons(m2_report, renderer):
    m2_report["candidates"] = []
    m2_report["counts"]["candidate_count"] = 0
    m2_report["evaluations"] = m2_report["evaluations"][1:]
    text = renderer(m2_report)
    assert "本期量价预候选为 0" in text
    assert "缺失分析日数据" in text
    assert "未通过条件或数据不足" in text


def test_html_escapes_source_text_and_does_not_expand_template_markers(m2_report):
    malicious = '<script>alert(1)</script>{{body}} & "quoted"'
    m2_report["title"] = malicious
    m2_report["evaluations"][0]["name"] = malicious
    html = render_m2_html(m2_report)
    parser = Tags()
    parser.feed(html)
    assert "script" not in parser.tags
    assert "&lt;script&gt;alert(1)&lt;/script&gt;{{body}}" in html
    assert parser.tags.count("section") == 7
    assert "Content-Security-Policy" in html
    assert "http://" not in html and "https://" not in html


def test_markdown_escapes_title_and_external_cells(m2_report):
    m2_report["title"] = "<script>x</script> [click](https://bad.example)"
    m2_report["evaluations"][0]["name"] = "a|b\n# title"
    text = render_m2_markdown(m2_report)
    assert "<script>" not in text
    assert "\\[click\\]\\(https://bad.example\\)" in text
    assert "a\\|b \\# title" in text


def test_audit_csv_all_stocks_exact_numeric_precision_conditions_and_bom(m2_report, tmp_path):
    path = tmp_path / "nested" / "audit.csv"
    assert write_audit_csv(m2_report, path) == 3
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["symbol"] for row in rows] == ["sh.600000", "sz.000001", "sh.600036"]
    assert rows[0]["display_close"] == "12.34567890123456789"
    assert rows[0]["avg_amount_cny"] == "50000000.0001"
    assert rows[0]["relative_return"] == "0.09"
    assert rows[1]["ma_short"] == "" and rows[1]["rank"] == ""
    assert rows[1]["condition_trend_status"] == "unknown"
    assert rows[1]["condition_trend_reason"] == "缺失分析日数据"
    assert rows[2]["condition_relative_status"] == "fail"
    assert rows[2]["relative_return"] == "-0.02"
    assert rows[0]["snapshot_id"] == "snapshot-123" and rows[0]["benchmark_symbol"] == "sh.000001"


@pytest.mark.parametrize("text", ["=HYPERLINK(1)", "+SUM(1)", "-SUM(1)", "@SUM(1)", "\t=1+1", " \r=1+1"])
def test_audit_csv_neutralizes_external_formula_text(m2_report, tmp_path, text):
    m2_report["evaluations"][0]["name"] = text
    path = tmp_path / "audit.csv"
    write_audit_csv(m2_report, path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        first = next(csv.DictReader(handle))
    assert first["name"] == "'" + text


@pytest.mark.parametrize("value", ["NaN", "Infinity", "=1+1", "+SUM(1)"])
def test_audit_csv_rejects_invalid_numeric_cells(m2_report, tmp_path, value):
    m2_report["evaluations"][0]["ma_short"] = value
    with pytest.raises(ValueError):
        write_audit_csv(m2_report, tmp_path / "audit.csv")


def test_rendering_does_not_mutate_input(m2_report, tmp_path):
    before = deepcopy(m2_report)
    render_m2_html(m2_report)
    render_m2_markdown(m2_report)
    write_audit_csv(m2_report, tmp_path / "audit.csv")
    assert m2_report == before


@pytest.mark.parametrize("field,value", [("mode", "demo"), ("verification_kind", "unknown"), ("timezone", "UTC")])
def test_reject_ambiguous_or_demo_context(m2_report, field, value):
    m2_report[field] = value
    with pytest.raises(ValueError):
        render_m2_html(m2_report)


def test_real_input_label_only_when_explicit(m2_report):
    m2_report["verification_kind"] = "local_real_data"
    text = render_m2_html(m2_report)
    assert "本地真实行情" in text
    assert "本地 SQLite 真实行情冻结快照" in text
    assert "OFFLINE TEST" not in text


def test_non_trading_day_not_called_current_market(m2_report):
    m2_report.update(status="non_trading_day", actual_market_date=None, candidates=[], evaluations=[])
    text = render_m2_markdown(m2_report)
    assert "非交易日，不生成当日行情结论" in text
    assert "本期量价预候选为 0" in text


@pytest.mark.parametrize("renderer", [render_m2_html, render_m2_markdown])
def test_labels_and_formulas_follow_frozen_config(m2_report, renderer):
    m2_report["strategy_config"].update(ma_short_days=10, ma_long_days=30, return_days=5, amount_days=12,
                                        min_history_trading_days=150, max_candidates=4, min_avg_amount_cny="12345678")
    text = renderer(m2_report)
    for expected in ["MA10", "MA30", "C(T-5)", "6 个价格点", "12 日平均成交额", "150 个交易日", "12,345,678.00 元", "4 个量价预候选"]:
        if renderer is render_m2_markdown:
            expected = expected.replace("(", "\\(").replace(")", "\\)")
        assert expected in text
