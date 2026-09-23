"""Offline regressions for truthful, bounded and read-only dashboard views."""
from copy import deepcopy
import hashlib
import socket

import pytest

from ashare_daily.viewer_dashboard import cards_html, focus_rows, _number
from test_m4_viewer import app, texts
from test_observation_daily import publish, report_inputs, STAMP
from ashare_daily.reports.observation import build_report


def report(states):
    return build_report(*report_inputs(states), planned_cutoff=STAMP, generated_at=STAMP)


def test_formal_ranking_is_not_replaced_by_pending_or_reference_scores():
    data = report([("pass", "pass")] * 7 + [("pass", "pending")])
    data["reference_review"] = {"records": [{"security_id": "TEST-7", "score": 999,
        "baseline_technical_status": "pass", "eligibility_status": "pending", "status": "available", "symbol": "sh.600008"}]}
    rows, pending = focus_rows(data)
    assert not pending
    assert rows == data["observations"][:5]
    assert "TEST-7" not in [r["security_id"] for r in rows]


def test_pending_shadow_view_matches_frozen_auxiliary_order_and_excludes_failures():
    data = report([("pass", "pending")] * 6 + [("pass", "fail")])
    data["reference_review"] = {"records": [{"security_id": r["security_id"], "symbol": r["symbol"],
        "score": 90 - i, "baseline_technical_status": "pass", "eligibility_status": r["eligibility_status"],
        "status": "available"} for i, r in enumerate(data["evaluations"])]}
    before = deepcopy(data)
    rows, pending = focus_rows(data)
    assert pending and len(rows) == 5
    assert [r["security_id"] for r in rows] == [f"TEST-{i}" for i in range(5)]
    assert data == before and data["counts"]["observation_count"] == 0


def test_missing_values_are_not_zero_and_relative_return_uses_percentage_points():
    assert _number(None) == "未取得"
    assert _number("NaN") == "未取得"
    assert _number("0.1", scale=100, suffix=" pp", signed=True) == "+10.00 pp"


def test_card_missing_relative_return_has_no_directional_color():
    data = report([("pass", "pass")])
    row = data["observations"][0]
    row["metrics"]["relative_return_20"] = None
    html = cards_html(data, [row], False)
    assert '<strong class="brief-neutral">未取得</strong>' in html
    assert 'class="brief-up"' not in html and 'class="brief-down"' not in html


def test_cards_escape_all_archived_text_including_markdown_and_attribute_payloads():
    data = report([("pass", "pending")])
    row = data["pending"][0]
    row["name"] = '<img src="https://example.invalid/track" onerror="bad()">'
    row["company_risk_notice"] = "</p><script>bad()</script>"
    row["technical_conditions"] = [{"id": "trend", "status": "pass", "label": "<iframe src='bad'>"}]
    html = cards_html(data, [row], True)
    assert "<img" not in html and "<script" not in html and "<iframe" not in html
    assert "&lt;img" in html and "&lt;script&gt;" in html and "&lt;iframe" in html
    assert "资格待查" in html and "原规则通过" not in html


def test_overview_escapes_counts_even_when_legacy_contract_accepts_text():
    from ashare_daily.viewer_dashboard import _summary
    from streamlit.testing.v1 import AppTest
    data = report([("pass", "pending")])
    data["counts"]["technical_pass_count"] = '<img src="https://example.invalid/tracker">'
    # Invoke the real renderer with Streamlit in its normal script context.
    shown = AppTest.from_string("import streamlit as st\nfrom ashare_daily.viewer_dashboard import _summary\n_summary(st, " + repr(data) + ")").run()
    assert not shown.exception
    html = shown.markdown[0].value
    assert "<img" not in html and "&lt;img" in html


def test_summary_converts_timezone_and_does_not_invent_pending_watchlist():
    from streamlit.testing.v1 import AppTest
    data = report([("fail", "pass")])
    data["actual_generated_at"] = "2026-09-11T13:05:00+00:00"
    shown = AppTest.from_string("import streamlit as st\nfrom ashare_daily.viewer_dashboard import _summary\n_summary(st, " + repr(data) + ")").run()
    assert not shown.exception
    assert "查看未通过原因与数据缺口" in shown.markdown[0].value
    assert "先看待核查观察" not in shown.markdown[0].value
    assert "21:05:00" in shown.caption[0].value


def test_views_are_lazy_search_is_bounded_and_exports_preserve_archive(tmp_path, monkeypatch):
    publish(tmp_path, [("pass", "pending")] * 120)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("viewer must not connect"))
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    shown = app(tmp_path / "outputs")
    assert not shown.exception
    assert shown.radio(key="observation_view").value == "今日总览"
    assert not shown.dataframe and not shown.get("download_button")
    assert "正式候选为0" in "\n".join(item.value for item in shown.markdown)
    assert "资格待查，不是正式推荐" in texts(shown)
    shown.radio(key="observation_view").set_value("筛选明细").run()
    assert not shown.exception
    assert len(shown.dataframe[0].value) == 50
    shown.text_input(key="observation_search").set_value("600120").run()
    assert len(shown.dataframe[0].value) == 1
    shown.radio(key="observation_view").set_value("导出").run()
    assert not shown.exception and len(shown.get("download_button")) == 4
    shown.radio(key="observation_view").set_value("原文与证据").run()
    assert not shown.exception and len(shown.dataframe) == 1
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
