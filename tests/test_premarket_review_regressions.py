from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from test_m3_evidence import material, claim
from test_sector_observation_model import inputs, fake_client, output, rewrite_bundle, run
from test_premarket_daily import Clock, Services, setup, invoke
from test_premarket_candidates import preselect


def test_full_text_model_adapter_keeps_authorized_company_subject(inputs):
    from ashare_daily.research.evidence import build_evidence
    from ashare_daily.premarket.events import validate_events
    row = {k:v for k,v in inputs["evidence"].items() if k not in {"evidence_id", "content_hash", "content_version"}}
    sentence = "浦发银行（600000）发布服务说明。"
    row.update(content="OFFLINE TEST 这是人工合成测试资料。" + sentence*10,
        first_seen_at="2025-04-29T10:00:00+08:00", fetched_at="2025-04-29T10:00:00+08:00",
        security_associations=[{"symbol": "sh.600000", "name": "浦发银行", "basis_quote": sentence, "association_type": "explicit_subject"}])
    row = build_evidence(**row)
    rewrite_bundle(inputs, lambda bundle: bundle.update(evidence=[row]))
    proposed = output(row, text=sentence, symbol="sh.600000", citations=[{"evidence_id": row["evidence_id"], "quote": sentence, "locator": row["raw_locator"]}])
    client, seen = fake_client(proposed)
    entities = {"sh.600000": {"security_id": "a", "name": "浦发银行", "sector_ids": ["finance"]}}
    context = run(inputs, client=client, context_scope="premarket", allowed_entities=entities)
    assert context["analysis"]["accepted_count"] == 1
    accepted = [{k:c[k] for k in proposed["claims"][0]} for c in context["analysis"]["accepted_claims"]]
    events = validate_events(accepted, [row], cutoff="2025-04-30T23:59:59+08:00", entities=entities)
    assert events["events"][0]["security_ids"] == ["a"]
    assert "technical_score" not in str(seen)


def test_negated_full_sentence_cannot_become_adverse_by_substring():
    from ashare_daily.premarket.events import validate_events
    sentence = "监管部门明确表示没有处罚该机构。"
    item = material(content="OFFLINE TEST 合成资料。" + sentence, security_associations=[],
        first_seen_at="2026-10-12T08:40:00+08:00", fetched_at="2026-10-12T08:40:00+08:00")
    proposed = claim(item, claim_type="counterevidence", symbol=None, text="处罚该机构", risks=[],
        citations=[{"evidence_id": item["evidence_id"], "quote": sentence, "locator": item["raw_locator"]}])
    result = validate_events([proposed], [item], cutoff="2026-10-12T08:55:00+08:00", entities={})
    assert not any(e["risk_status"] == "adverse" for e in result["events"])


def test_model_adapter_preserves_validated_background_annotations():
    from ashare_daily.premarket.model import extract_context_events
    sentence = "监管部门处罚该机构。"
    item = material(content="OFFLINE TEST 合成资料。" + sentence, security_associations=[],
        first_seen_at="2026-10-12T08:40:00+08:00", fetched_at="2026-10-12T08:40:00+08:00")
    proposed = claim(item, claim_type="counterevidence", symbol=None, text=sentence, risks=[],
        citations=[{"evidence_id": item["evidence_id"], "quote": sentence, "locator": item["raw_locator"]}])
    result = extract_context_events({"evidence_catalog": [{**item, "is_background": True}],
        "analysis": {"accepted_claims": [proposed]}}, [item], cutoff="2026-10-12T08:55:00+08:00", entities={})
    assert result["events"][0]["is_background"] is True
    assert result["events"][0]["risk_status"] == "unknown"


def test_financial_risk_and_missing_financials_reach_recommendation_gate():
    from ashare_daily.premarket.company import merge_company_reviews
    from ashare_daily.premarket.candidates import finalize_candidates
    selected = preselect()
    announcements = {r["security_id"]: {"status": "ready", "risk_status": "clear", "evidence_ids": ["e1"]} for r in selected["candidates"]}
    financial = {"records": [{"security_id": "id22", "status": "available", "profit": {"netProfit": "-2"},
        "cash_flow": {"CAToAsset": "1"}, "risk_flags": ["归母净利润为负"], "gaps": [], "provenance": {}}]}
    merged = merge_company_reviews(selected, announcements, financial)
    result = finalize_candidates(selected, company_reviews=merged, stance={"stance": "attack"}, events=[])
    assert result["focus_candidates"] == []
    row = next(r for r in result["pending_candidates"] if r["security_id"] == "id22")
    assert row["company_review"]["financial_risk_flags"] == ["归母净利润为负"]
    assert row["company_review"]["financial_facts"]["profit"]["netProfit"] == "-2"


def test_recovery_after_publication_before_journal_does_not_replace_good_edition(tmp_path, monkeypatch):
    from ashare_daily.operations import premarket
    from ashare_daily.premarket.archive import scan_briefs
    cfg, clock = setup(tmp_path), Clock()
    services = Services(clock)
    real = premarket.publish_brief
    crashed = [False]
    def interrupted(*args, **kwargs):
        result = real(*args, **kwargs)
        if result["status"] == "published" and not crashed[0]:
            crashed[0] = True
            raise KeyboardInterrupt("simulated power loss")
        return result
    monkeypatch.setattr(premarket, "publish_brief", interrupted)
    with pytest.raises(KeyboardInterrupt): invoke(tmp_path, cfg, services, clock)
    before = scan_briefs(tmp_path / "outputs", now=clock.now())[0]
    clock.sleep(2)
    result = invoke(tmp_path, cfg, services, clock)
    after = scan_briefs(tmp_path / "outputs", now=clock.now())[0]
    assert result["status"] == "reused"
    assert len(before) == len(after) == 1
    assert after[0]["report"]["market"]["sample_count"] == 5


def test_failed_news_does_not_advance_watermark_next_day(tmp_path):
    cfg, clock = setup(tmp_path), Clock()
    class AcrossDays(Services):
        windows = []
        def market(self, schedule, directory, seconds, run_id):
            result = super().market(schedule, directory, seconds, run_id)
            self.clock.value = datetime.fromisoformat(schedule["report_date"] + "T08:50:00+08:00")
            result["preselection"]["market_as_of_date"] = schedule["market_as_of_date"]
            return result
        def news(self, schedule, window, prepared, directory, seconds):
            self.windows.append(window["start_at"])
            self.clock.value = datetime.fromisoformat(schedule["report_date"] + "T08:55:00+08:00")
            return {"coverage": {"ready": False}, "company_reviews": {}}
    services = AcrossDays(clock)
    invoke(tmp_path, cfg, services, clock)
    clock.value = datetime.fromisoformat("2026-10-13T08:30:00+08:00")
    invoke(tmp_path, cfg, services, clock)
    assert services.windows == ["2026-10-09T08:55:00+08:00", "2026-10-09T08:55:00+08:00"]
