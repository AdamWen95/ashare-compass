"""Template completeness, escaping untrusted text and rejecting broken reports."""

from html import escape

from pydantic import ValidationError
import pytest

from ashare_daily.demo import build_demo_report
from ashare_daily.reports.publish import publish_report
from ashare_daily.reports.render import render_html, render_markdown
from ashare_daily.schemas import DailyReport


@pytest.mark.parametrize("render", [render_markdown, render_html])
def test_report_contains_required_sections_and_demo_notice(demo_report, render):
    content = render(demo_report)
    for heading in ("DEMO", "人工合成", "数据状态", "市场复盘", "重要消息", "重点方向", "选股策略", "候选观察池", "待验证事项", "证据索引"):
        assert heading in content
    assert "5.00%" in content
    assert demo_report.snapshot_id in content


def test_html_escapes_untrusted_text_and_has_no_external_resources(demo_report):
    payload = '<script>alert("DEMO & injected")</script>'
    data = demo_report.model_dump()
    data["important_news"][0]["text"] = payload
    data["evidence"][0]["title"] = payload
    report = DailyReport.model_validate(data)
    content = render_html(report)
    assert payload not in content
    assert escape(payload) in content
    assert "<script" not in content.lower()
    assert 'src="http' not in content.lower()
    assert 'href="http' not in content.lower()


@pytest.mark.parametrize("render", [render_markdown, render_html])
def test_zero_candidates_renders_a_complete_report(scenario_date, generated_at, render):
    report = build_demo_report(scenario_date, generated_at=generated_at, empty_candidates=True)
    content = render(report)
    assert "DEMO" in content
    assert "候选观察池" in content
    assert "证据索引" in content
    assert "无候选" in content or "候选为空" in content or "零候选" in content


@pytest.mark.parametrize("render", [render_markdown, render_html])
def test_render_revalidates_nested_mutations(demo_report, render):
    # Pydantic list mutations bypass assignment validation; publication is a boundary.
    demo_report.candidates[0].metric_ids.append("M-DOES-NOT-EXIST")
    with pytest.raises(ValidationError, match="不存在"):
        render(demo_report)


def test_invalid_report_cannot_create_any_publication(demo_report, tmp_path):
    demo_report.candidates[0].evidence_ids.append("E-DOES-NOT-EXIST")
    with pytest.raises(ValidationError, match="不存在"):
        publish_report(demo_report, tmp_path)
    assert not list(tmp_path.iterdir())
