"""Combine validated financial facts and announcement checks by security identity."""
from copy import deepcopy


def merge_company_reviews(preselection, announcements, financial_review):
    records = (financial_review or {}).get("records", [])
    financials = {r["security_id"]: r for r in records}
    if len(financials) != len(records):
        raise ValueError("premarket_financial_identity_duplicate")
    result = {}
    for candidate in preselection["candidates"]:
        identity = candidate["security_id"]
        review = deepcopy(announcements.get(identity, {"status": "partial", "risk_status": "unknown", "evidence_ids": []}))
        financial = financials.get(identity, {})
        flags, gaps = list(financial.get("risk_flags", [])), list(financial.get("gaps", []))
        if not financial:
            gaps.append("本期未取得财务资料，不能解释为没有风险")
        review.update(financial_status=financial.get("status", "unavailable"), financial_risk_flags=flags,
            financial_gaps=gaps, financial_facts={key: deepcopy(financial.get(key)) for key in ("profit", "cash_flow")},
            financial_provenance=deepcopy(financial.get("provenance", {})))
        if financial.get("status") != "available" or financial.get("profit") is None or financial.get("cash_flow") is None:
            review["status"] = "partial"
        if flags and review.get("risk_status") != "adverse":
            review["risk_status"] = "review_required"
        result[identity] = review
    return result
