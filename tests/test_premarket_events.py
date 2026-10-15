from copy import deepcopy

import pytest

from test_m3_evidence import material, claim

END = "2026-10-12T08:55:00+08:00"
ENTITIES = {"sh.600000": {"security_id": "security-a", "name": "浦发银行", "sector_ids": ["finance"]}}


def evidence(**changes):
    return material(first_seen_at="2026-10-12T08:40:00+08:00", fetched_at="2026-10-12T08:40:00+08:00", **changes)


def check(item, text=None):
    from ashare_daily.premarket.events import validate_events
    proposed = claim(item, risks=[], **({"text": text} if text else {}))
    return validate_events([proposed], [item], cutoff=END, entities=ENTITIES)


def test_cited_fact_is_associated_locally_not_declared_a_beneficiary():
    result = check(evidence())
    assert result["events"][0]["security_ids"] == ["security-a"]
    assert result["events"][0]["risk_status"] == "unknown"
    assert result["events"][0]["semantic_review_required"] is True


@pytest.mark.parametrize("mutation", ["late_seen", "hash", "quote", "wrong_subject", "metadata"])
def test_unusable_evidence_is_not_an_event(mutation):
    item = evidence()
    text = None
    if mutation == "late_seen": item["first_seen_at"] = item["fetched_at"] = "2026-10-12T08:56:00+08:00"
    if mutation == "hash": item["content"] += "未归档更改"
    if mutation == "quote": text = "没有在原文出现的结论"
    if mutation == "wrong_subject": item["security_associations"][0]["name"] = "另一家公司"
    if mutation == "metadata": item["content_type"] = "metadata_only"
    assert not check(item, text)["events"]


def test_inference_remains_pending_and_cannot_become_automatic_risk():
    from ashare_daily.premarket.events import validate_events
    item = evidence()
    proposed = claim(item, claim_type="inference", text="可能存在风险，尚需确认", risks=[])
    result = validate_events([proposed], [item], cutoff=END, entities=ENTITIES)
    assert result["events"][0]["risk_status"] == "unknown"


def test_revision_suppresses_old_version_in_same_batch():
    from ashare_daily.premarket.events import validate_events
    old = evidence()
    new = evidence(revision_of=old["evidence_id"], content=old["content"] + "更正说明。")
    result = validate_events([claim(old, risks=[]), claim(new, claim_id="c2", risks=[])], [old, new], cutoff=END, entities=ENTITIES)
    assert len(result["events"]) == 1
    assert result["events"][0]["event_id"] == "c2"
