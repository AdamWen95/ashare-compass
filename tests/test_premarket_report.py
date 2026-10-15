from datetime import datetime
from pathlib import Path

import pytest

from test_premarket_timing import resolve
from test_premarket_market import snapshot, market_inputs, decide
from test_premarket_candidates import preselect

STAMP = "2026-10-12T08:59:00+08:00"
DUE = datetime.fromisoformat("2026-10-12T09:00:00+08:00")


def make_brief():
    from ashare_daily.premarket.report import build_brief
    from ashare_daily.premarket.candidates import finalize_candidates
    market = snapshot(market_inputs())
    market["market_as_of_date"] = "2026-10-09"
    market["benchmark"]["market_as_of_date"] = "2026-10-09"
    stance = decide(market, news_ready=False)
    candidates = finalize_candidates(preselect(), company_reviews={}, stance=stance, events=[])
    return build_brief(schedule=resolve(), market=market, stance=stance, candidates=candidates,
        events=[], coverage={"ready": False, "roles": {}}, previous=None, generated_at=STAMP)


def test_report_dates_and_no_account_fields():
    report = make_brief()
    assert report["report_date"] == report["target_session_date"] == "2026-10-12"
    assert report["market_as_of_date"] == "2026-10-09"
    assert report["stance"]["reason_code"] == "trend_breadth_activity"
    assert report["stance"]["limitations"] and report["research_status"] == "partial"
    assert report["score_used_for_priority"] is True
    assert len(report["screening_audit"]) == 25
    assert not {"quantity", "position_size", "buy_price", "win_rate"} & report.keys()


def test_mismatched_market_date_cannot_build():
    from ashare_daily.premarket.report import build_brief
    with pytest.raises(ValueError, match="date"):
        build_brief(schedule=resolve(), market=snapshot(market_inputs()), stance={"stance": "hold"},
            candidates={}, events=[], coverage={}, previous=None, generated_at=STAMP)


def test_early_draft_hidden_and_due_publication_roundtrips(tmp_path):
    from ashare_daily.premarket.archive import publish_brief, scan_briefs
    report = make_brief()
    draft = publish_brief(tmp_path, report, now=datetime.fromisoformat(STAMP))
    assert draft["status"] == "staged"
    assert scan_briefs(tmp_path / "outputs", now=DUE)[0] == []
    result = publish_brief(tmp_path, report, now=DUE)
    archives, problems = scan_briefs(tmp_path / "outputs", now=DUE)
    assert not problems and len(archives) == 1
    assert archives[0]["report"]["published_at"] == DUE.isoformat()
    assert result["report_id"] == archives[0]["report"]["report_id"]
    assert publish_brief(tmp_path, report, now=DUE)["reused"] is True


def test_tampered_export_cannot_be_shown(tmp_path):
    from ashare_daily.premarket.archive import publish_brief, scan_briefs
    result = publish_brief(tmp_path, make_brief(), now=DUE)
    (Path(result["directory"]) / "premarket_brief.md").write_text("tampered", encoding="utf-8")
    archives, problems = scan_briefs(tmp_path / "outputs", now=DUE)
    assert archives == [] and len(problems) == 1


def test_untrusted_stock_text_is_not_executable_html(tmp_path):
    from ashare_daily.premarket.archive import publish_brief
    report = make_brief()
    report["pending_candidates"][0]["name"] = '<script>alert("x")</script>'
    result = publish_brief(tmp_path, report, now=DUE)
    html = (Path(result["directory"]) / "premarket_brief.html").read_text(encoding="utf-8")
    assert '<script>alert("x")</script>' not in html
    assert "&lt;script&gt;" in html


def test_report_on_later_day_is_late_and_not_relabelled_today(tmp_path):
    from ashare_daily.premarket.archive import publish_brief, scan_briefs
    when = datetime.fromisoformat("2026-10-13T09:00:00+08:00")
    publish_brief(tmp_path, make_brief(), now=when)
    report = scan_briefs(tmp_path / "outputs", now=when)[0][0]["report"]
    assert report["report_date"] == "2026-10-12"
    assert report["publication_status"] == "late"
