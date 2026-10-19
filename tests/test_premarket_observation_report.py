"""Independent market conclusions and frozen, evidence-bound watch reports."""
from copy import deepcopy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from test_premarket_market import market_inputs, snapshot
from test_premarket_report import DUE, STAMP, make_brief
from test_premarket_timing import resolve
from premarket_ui_helpers import morning_content


def observation_inputs(*, empty=False):
    market = snapshot(market_inputs((-1, -1, -1, -1, 1)))
    market["market_as_of_date"] = market["benchmark"]["market_as_of_date"] = "2026-10-09"
    direction = {"sector_id": "relative-a", "name": "相对行业", "selected_rank": 1,
        "direction_kind": "relative_strength", "relative_metrics": {
            "mean_change_pct": "-0.2", "median_change_pct": "-0.1", "advancing_fraction": "0.4",
            "market_mean_change_pct": "-0.6", "market_advancing_fraction": "0.2",
            "excess_market_pp": "0.4", "absolute_up": False, "member_count": 5}}
    row = {"security_id": "watch-id", "symbol": "sh.600000", "name": "观察公司",
        "primary_sector_id": "relative-a", "sector_ids": ["relative-a"],
        "technical_status": "fail", "eligibility_status": "pending", "technical_score": None,
        "research_status": "watch", "reasons": ["单日相对强势，历史趋势未确认"],
        "watch_conditions": ["若后续仍强于行业，继续核验历史趋势"],
        "invalidation_conditions": ["若相对优势消失，观察依据失效"],
        "observation_risks": ["单日强弱不代表上涨趋势"],
        "company_review": {"status": "partial", "gaps": ["公司公告待核验"], "financial_gaps": ["盈利质量待核验"]},
        "relative_observation": {"sector_id": "relative-a", "sector_mean_change_pct": "-0.2",
            "market_mean_change_pct": "-0.6", "stock_change_pct": "-0.1", "excess_market_pp": "0.5",
            "excess_sector_pp": "0.1", "amount_cny": "80000000", "market_as_of_date": "2026-10-09",
            "kind": "single_session_relative_strength"}}
    candidates = {"watch_candidates": [] if empty else [row], "focus_candidates": [], "pending_candidates": [],
        "preselection": {"observation_only": True, "rule_version": "premarket-observation-v1",
            "selection_id": None if empty else "observation-selection", "market_as_of_date": "2026-10-09",
            "directions": [] if empty else [direction], "audit": [] if empty else [deepcopy(row)]}}
    return market, candidates


def observation_brief(*, empty=False, previous=None):
    from ashare_daily.premarket.decisions import decide_stance
    from ashare_daily.premarket.report import build_brief
    market, candidates = observation_inputs(empty=empty)
    stance = decide_stance(market, news_ready=False, has_direction=not empty, independent=True)
    return build_brief(schedule=resolve(), market=market, stance=stance, candidates=candidates,
        events=[], coverage={"ready": False}, previous=previous, generated_at=STAMP)


@pytest.mark.parametrize("changes,expected", [((1, 1, 1, -1, 0), "attack"), ((-1, -1, -1, -1, 1), "defend")])
def test_independent_stance_is_identical_with_or_without_observation_direction(changes, expected):
    from ashare_daily.premarket.decisions import decide_stance
    market = snapshot(market_inputs(changes))
    without = decide_stance(market, news_ready=False, has_direction=False, independent=True)
    with_direction = decide_stance(market, news_ready=False, has_direction=True, independent=True)
    assert without == with_direction
    assert without["stance"] == expected
    assert without["rule_version"] == "premarket-stance-v3"
    assert "合格关注方向" not in "；".join(without["reasons"])


def test_defensive_report_keeps_relative_decliners_without_claiming_positive_trend():
    report = observation_brief()
    assert report["schema_version"] == "premarket-brief-v3"
    assert report["rule_versions"]["recommendation"] == "premarket-observation-v1"
    assert report["stance"]["stance"] == "defend" and report["research_status"] == "partial"
    assert len(report["watch_candidates"]) == 1 and report["focus_candidates"] == []
    assert "相对抗跌" in report["directions"][0]["reason"]
    assert "上涨广度通过规则" not in report["directions"][0]["reason"]
    assert report["observation_selection_id"] == "observation-selection"


@pytest.mark.parametrize("mutation", ["duplicate", "too_many", "identity", "symbol", "direction", "date",
    "relative_number", "missing_risk", "version", "stance", "legacy_bucket", "market_binding",
    "audit_status", "name", "known_adverse", "failed_eligibility", "insufficient_amount"])
def test_v3_rejects_invalid_watch_contract(mutation):
    from ashare_daily.premarket.report import validate_brief
    report = observation_brief()
    row = report["watch_candidates"][0]
    if mutation == "duplicate": report["watch_candidates"].append(deepcopy(row))
    elif mutation == "too_many": report["watch_candidates"] = [dict(row, security_id=f"id{i}") for i in range(6)]
    elif mutation == "identity": row["security_id"] = "other"
    elif mutation == "symbol": row["symbol"] = "sz.000001"
    elif mutation == "direction": row["relative_observation"]["sector_id"] = "other"
    elif mutation == "date": row["relative_observation"]["market_as_of_date"] = "2026-10-08"
    elif mutation == "relative_number": row["relative_observation"]["excess_market_pp"] = "NaN"
    elif mutation == "missing_risk": row["observation_risks"] = []
    elif mutation == "version": report["rule_versions"]["recommendation"] = "premarket-recommendation-v1"
    elif mutation == "stance": report["stance"]["reasons"] = ["伪造结论"]
    elif mutation == "legacy_bucket": report["pending_candidates"] = [deepcopy(row)]
    elif mutation == "market_binding": report["directions"][0]["relative_metrics"]["market_mean_change_pct"] = "-2"
    elif mutation == "audit_status": row["technical_status"] = "pass"
    elif mutation == "name": row["name"] = "冒名公司"
    elif mutation == "known_adverse": row["company_review"]["risk_status"] = "adverse"
    elif mutation == "failed_eligibility": row["eligibility_status"] = "fail"
    elif mutation == "insufficient_amount": row["relative_observation"]["amount_cny"] = "49999999"
    with pytest.raises(ValueError): validate_brief(report)


def test_builder_rejects_observation_flag_on_legacy_preselection():
    from ashare_daily.premarket.decisions import decide_stance
    from ashare_daily.premarket.report import build_brief
    market, candidates = observation_inputs()
    candidates["preselection"]["rule_version"] = "premarket-recommendation-v1"
    stance = decide_stance(market, news_ready=False, has_direction=True, independent=True)
    with pytest.raises(ValueError, match="observation"):
        build_brief(schedule=resolve(), market=market, stance=stance, candidates=candidates,
            events=[], coverage={"ready": False}, previous=None, generated_at=STAMP)


def test_real_observation_candidates_flow_through_v3_report():
    from test_premarket_observation_candidates import inputs, rebind
    from ashare_daily.premarket.candidates import finalize_candidates, preselect_candidates
    from ashare_daily.premarket.decisions import decide_stance
    from ashare_daily.premarket.report import build_brief
    values = inputs()
    values[0]["target_date"] = values[2]["trade_date"] = "2026-10-09"
    for member in values[0]["members"]:
        member["relative_observation"]["market_as_of_date"] = "2026-10-09"
    rebind(values)
    values[1][0].update(technical_status="fail", eligibility_status="pending")
    market, _ = observation_inputs()
    stance = decide_stance(market, news_ready=False, has_direction=True, independent=True)
    selected = preselect_candidates(*values)
    candidates = finalize_candidates(selected, company_reviews={}, stance=stance, events=[])
    report = build_brief(schedule=resolve(), market=market, stance=stance, candidates=candidates,
        events=[], coverage={"ready": False}, previous=None, generated_at=STAMP)
    assert report["stance"]["stance"] == "defend" and len(report["watch_candidates"]) == 5
    assert all(row["observation_status"] == "conditional" for row in report["watch_candidates"])
    assert "a0" in {row["security_id"] for row in report["watch_candidates"]}


def test_v3_keeps_empty_partial_report_readable():
    from ashare_daily.premarket.report import validate_brief
    report = observation_brief(empty=True)
    validate_brief(report)
    assert report["watch_candidates"] == [] and report["stance"]["stance"] == "defend"


def test_decimal_json_rounding_does_not_reject_valid_relative_facts():
    from ashare_daily.premarket.report import validate_brief
    report = observation_brief()
    facts = report["directions"][0]["relative_metrics"]
    facts.update(mean_change_pct=-0.3333333333333333, excess_market_pp=0.26666666666666666)
    report["watch_candidates"][0]["relative_observation"].update(
        sector_mean_change_pct=-0.3333333333333333, excess_sector_pp=0.23333333333333334)
    validate_brief(report)


def test_display_rounds_relative_numbers_and_converts_amount_without_rewriting_json(tmp_path):
    import json
    from ashare_daily.premarket.archive import publish_brief, scan_briefs
    from ashare_daily.premarket.report import direction_facts, observation_facts
    report = observation_brief()
    direction = report["directions"][0]
    direction["relative_metrics"].update(mean_change_pct=-0.3333333333333333,
        excess_market_pp=0.26666666666666666)
    row = report["watch_candidates"][0]
    row["relative_observation"].update(sector_mean_change_pct=-0.3333333333333333,
        excess_sector_pp=0.23333333333333334, amount_cny=80000123.456)
    before = deepcopy(report)
    assert "行业平均 -0.33%" in "；".join(direction_facts(direction))
    assert "相对全市场 0.27 个百分点" in "；".join(direction_facts(direction))
    assert "相对行业 0.23 个百分点" in "；".join(observation_facts(row))
    assert "成交额 8000.01 万元" in "；".join(observation_facts(row))
    assert report == before
    publish_brief(tmp_path, report, now=DUE)
    archives, errors = scan_briefs(tmp_path / "outputs", now=DUE)
    assert not errors
    stored = json.loads(archives[0]["files"]["premarket_brief.json"])
    assert stored["directions"][0]["relative_metrics"] == before["directions"][0]["relative_metrics"]
    assert stored["watch_candidates"][0]["relative_observation"] == before["watch_candidates"][0]["relative_observation"]
    for name in ("premarket_brief.html", "premarket_brief.md"):
        text = archives[0]["files"][name].decode()
        assert "-0.3333333333333333" not in text and "8000.01 万元" in text


@pytest.mark.parametrize("mismatch", ["schema", "recommendation", "both"])
def test_cross_version_history_does_not_claim_comparable_watch_changes(mismatch):
    previous = observation_brief()
    previous["watch_candidates"][0]["security_id"] = "previous-watch"
    if mismatch in {"schema", "both"}: previous["schema_version"] = "premarket-brief-v2"
    if mismatch in {"recommendation", "both"}: previous["rule_versions"]["recommendation"] = "premarket-recommendation-v1"
    before = deepcopy(previous)
    report = observation_brief(previous=previous)
    assert report["changes_since_previous"] == {"comparable": False, "added_ids": [], "removed_ids": [], "previous_stance": None}
    assert report["directions"][0]["continuity"] == "本期相对表现观察"
    assert previous == before


def test_matching_versions_keep_real_watch_changes():
    previous = observation_brief()
    previous["watch_candidates"][0]["security_id"] = "previous-watch"
    report = observation_brief(previous=previous)
    assert report["changes_since_previous"] == {"comparable": True, "added_ids": ["watch-id"],
        "removed_ids": ["previous-watch"], "previous_stance": "defend"}
    assert report["directions"][0]["continuity"] == "延续观察"


def test_archive_exports_and_reader_show_watch_conditions_risks_and_numeric_evidence_once(tmp_path):
    from ashare_daily.premarket.archive import publish_brief, scan_briefs
    publish_brief(tmp_path, observation_brief(), now=DUE)
    archives, errors = scan_briefs(tmp_path / "outputs", now=DUE)
    assert not errors and len(archives) == 1
    for name in ("premarket_brief.html", "premarket_brief.md", "premarket_brief.json"):
        content = archives[0]["files"][name].decode()
        for fact in ("观察公司", "相对抗跌", "若后续仍强于行业", "观察依据失效", "单日强弱不代表上涨趋势", "盈利质量待核验"):
            assert fact in content
        if name.endswith((".html", ".md")):
            assert content.count("观察公司") == 2  # One watch card and one audit row.
            assert "0.50" in content and "0.10" in content and "8000.00 万元" in content
    script = f'''
from datetime import datetime
from pathlib import Path
import streamlit as st
from ashare_daily.premarket.archive import scan_briefs
from ashare_daily.viewer_premarket import render_morning
root = Path({str(tmp_path / 'outputs')!r})
now = datetime.fromisoformat("2026-10-12T09:00:00+08:00")
archives, errors = scan_briefs(root, now=now)
render_morning(st, archives, root, now=now, runs=[])
'''
    app = AppTest.from_string(script).run()
    assert not app.exception
    content=morning_content(app)
    text = ' '.join(content.parts)
    assert content.stock_cards==1 and text.count('观察公司')==2  # One card and one visible-on-expand audit row.
    for fact in ("相对抗跌", "0.50", "0.10", "8000.00 万元", "若后续仍强于行业", "观察依据失效", "单日强弱不代表上涨趋势", "盈利质量待核验"):
        assert fact in text
    assert len(app.get("download_button")) == 3


@pytest.mark.parametrize("legacy", [False, True])
def test_publishing_v3_does_not_rewrite_frozen_v1_v2_archive(tmp_path, legacy):
    from ashare_daily.premarket.archive import publish_brief, scan_briefs
    report = make_brief()
    if legacy:
        report["schema_version"] = "premarket-brief-v1"
        report["rule_versions"]["stance"] = "premarket-stance-v1"
        report["stance"] = {"rule_version": "premarket-stance-v1", "stance": "hold", "label": "按兵不动",
            "reasons": ["冻结历史规则"]}
    result = publish_brief(tmp_path, report, now=DUE)
    files = {path: path.read_bytes() for path in Path(result["directory"]).iterdir()}
    publish_brief(tmp_path, observation_brief(), now=DUE)
    archives, errors = scan_briefs(tmp_path / "outputs", now=DUE)
    assert not errors and len(archives) == 2
    assert all(path.read_bytes() == content for path, content in files.items())
