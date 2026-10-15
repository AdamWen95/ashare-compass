"""Strict as-observed event validation; quoted text is not semantic certainty."""
from __future__ import annotations

from copy import deepcopy
import re

from ..research.evidence import build_evidence, validate_claims
from ..research.contracts import Evidence
from .contracts import aware


def validate_events(claims: list[dict], evidence: list[dict], *, cutoff: str, entities: dict) -> dict:
    end, admitted, rejected = aware(cutoff), {}, []
    for item in evidence:
        try:
            value = build_evidence(**{k: item[k] for k in Evidence.model_fields if k in item})
            if aware(value["first_seen_at"]) > end or aware(value["fetched_at"]) > end:
                raise ValueError("first_seen_after_cutoff")
            if value["content_type"] == "metadata_only":
                raise ValueError("metadata_only")
            admitted[value["evidence_id"]] = {**value, "is_background": item.get("is_background", False)}
        except (ValueError, KeyError, TypeError):
            rejected.append({"evidence_id": item.get("evidence_id"), "reason": "evidence_not_verified_as_observed"})
    superseded = set()
    for value in admitted.values():
        old = admitted.get(value.get("revision_of"))
        if (old and old["source_id"] == value["source_id"] and old["original_url"] == value["original_url"]
                and aware(value["first_seen_at"]) >= aware(old["first_seen_at"])):
            superseded.add(old["evidence_id"])
    allowed = {k: v for k, v in admitted.items() if k not in superseded}
    events = []
    for claim in claims:
        try:
            checked = validate_claims({"claims": [claim]}, list(allowed.values()), entities, {}, cutoff, strict_counterevidence=True)
        except (ValueError, TypeError, KeyError):
            rejected.append({"claim_id": claim.get("claim_id"), "reason": "claim_invalid"})
            continue
        rejected.extend(checked["rejected_claims"])
        for value in checked["accepted_claims"]:
            identity = entities.get(value.get("symbol"), {})
            extract = value["claim_type"] in {"fact", "counterevidence"}
            cited = [allowed[c["evidence_id"]] for c in value["citations"]]
            # Conservative downgrade only: negation, future plans, revisions and
            # background never turn into an automatic adverse-event decision.
            sentences = [s.strip() for source in cited for s in re.findall(r"[^。！？\n]+[。！？]?", source["content"])
                         if value["text"].strip() in s]
            whole_sentence = bool(sentences) and all(value["text"].strip().rstrip("。！？") == s.rstrip("。！？") for s in sentences)
            adverse = (value["claim_type"] == "counterevidence" and whole_sentence and bool(identity)
                and not re.search(r"未|没有|否认|不涉及|无此|拟|计划|可能|将于|更正|撤回", " ".join(sentences))
                and not any(v.get("is_background") or v.get("revision_of") for v in cited))
            if value["claim_type"] == "counterevidence" and not whole_sentence:
                extract = False
            events.append({"event_id": value["claim_id"], "claim_type": value["claim_type"], "text": value["text"],
                "validation_status": "verified" if extract else "pending", "semantic_review_required": True,
                "risk_status": "adverse" if adverse else "unknown", "security_ids": [identity["security_id"]] if identity.get("security_id") else [],
                "impact_scope": "company" if identity else "unverified",
                "sector_ids": list(identity.get("sector_ids", [])), "citations": deepcopy(value["citations"]),
                "sources": [{k: v.get(k) for k in ("evidence_id", "source_id", "original_url", "published_at",
                    "publication_precision", "first_seen_at", "content_hash", "original_publisher")} for v in cited],
                "effective_at": None, "effective_time_status": "unverified",
                "is_background": any(v.get("is_background", False) for v in cited)})
    counts = {}
    for event in events:
        counts[event["event_id"]] = counts.get(event["event_id"], 0) + 1
    duplicates = {k for k, n in counts.items() if n > 1}
    rejected.extend({"claim_id": k, "reason": "duplicate_event_id"} for k in sorted(duplicates))
    events = [e for e in events if e["event_id"] not in duplicates]
    return {"events": events, "rejected": rejected, "superseded_evidence_ids": sorted(superseded),
            "status": "ready" if events and not rejected else "partial" if events else "unavailable"}
