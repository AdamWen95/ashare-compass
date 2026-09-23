"""Inspectable per-member issues and source-boundary counts, separate from gates."""
from collections import Counter
from pathlib import Path

from ashare_daily.operations.daily import atomic_json
from ashare_daily.universe import scope_boards


def write_acceptance_details(directory: Path, snapshot: dict) -> dict:
    members = snapshot["members"]
    sources = snapshot["source_manifests"]
    issues = []
    for row in members:
        reasons = list(row.get("classification_reasons", [])) + list(row.get("metadata_issues", []))
        for state, evidence in row.get("statuses", {}).items():
            if evidence.get("value") is None:
                reasons.append(state + ":" + evidence.get("unknown_reason", "unknown"))
        if row.get("discovery_classification") == "outside_ordinary_a":
            reasons.append("excluded_non_ordinary:" + row["security_type"])
        if reasons:
            issues.append({"security_id": row["security_id"], "provider": row["provider"],
                           "code": row["code"], "name": row["name"], "reasons": sorted(set(reasons)),
                           "evidence_id": row.get("evidence_id"), "security_type": row["security_type"]})
    report = {}
    for board in scope_boards(snapshot.get("scope", "all_a")):
        manifests = [s for s in sources if board in s.get("coverage_boards", [])]
        keys = {(s["provider"], s["dataset"]) for s in manifests}
        related = [r for r in members if r.get("board") == board or
                   (r.get("unverified_metadata", {}).get("board") == board)]
        # Unknown rows are scoped by source dataset; a source spanning two boards
        # conservatively makes both counts unknown if its metadata is unresolved.
        providers = {k[0] for k in keys}
        unknown = [r for r in members if r["provider"] in providers and
                   r.get("discovery_classification") == "metadata_unknown" and
                   r.get("unverified_metadata", {}).get("board", "unknown") in {board, "unknown"}]
        labels = {":".join(k) for k in keys}
        blocking = [b for b in snapshot["blockers"] if any(label in b for label in labels) or
                    b.endswith(":" + board) or b.startswith(("identity_conflict:", "duplicate_source_code:",
                    "conflicting_security_metadata:", "alias_conflict:", "previous_members_missing_unexplained:")) or
                    b in {"calendar_unverified", "requested_trade_date_mismatch", "cutoff_not_reached"}]
        listing_reasons = {"listing_date_unknown", "listing_status_unknown", "delisting_date_unknown", "invalid_listing_date"}
        if any(r.get("discovery_classification") == "ordinary_a" and
               listing_reasons.intersection(r.get("classification_reasons", [])) for r in related):
            blocking.append("board_listing_validity_unverified:" + board)
        complete = bool(manifests) and all(s.get("complete") is True and s.get("authoritative") is True and
                   s.get("permission_status") == "approved" and not s.get("errors") for s in manifests)
        accepted = complete and not unknown and not blocking and snapshot.get("calendar_verified") is True
        report[board] = {"ordinary_a_count": snapshot["board_counts"][board] if accepted else None,
                         "observed_verified_ordinary_a": snapshot["board_counts"][board],
                         "source_boundary_verified": complete, "board_metadata_verified": accepted,
                         "unknown_metadata_in_source": len(unknown) if complete else None,
                         "source_dates": [{"provider": s["provider"], "dataset": s["dataset"],
                                           "source_as_of_date": s.get("source_as_of_date", s.get("as_of_date")),
                                           "source_business_date": s.get("source_business_date", s.get("source_as_of_date")),
                                           "observed_at": s.get("observed_at"),
                                           "temporal_basis": s.get("temporal_basis"),
                                           "expected_records": s.get("expected_records"),
                                           "expected_pages": s.get("expected_pages")} for s in manifests],
                         "blockers": blocking + [error for s in manifests for error in s.get("errors", [])]}
    details = {"snapshot_id": snapshot["snapshot_id"], "requested_date": snapshot["requested_date"],
               "scope": snapshot.get("scope", "all_a"),
               "excluded_boards": snapshot.get("excluded_boards", []),
               "collection_ready": snapshot.get("collection_ready", False),
               "research_ready": snapshot.get("research_ready", False),
               "universe_verified": snapshot["universe_verified"], "board_acceptance": report,
               "security_type_counts": dict(Counter(m["security_type"] for m in members)),
               "global_blockers": snapshot["blockers"], "changes": snapshot["changes"],
               "member_issues": issues, "price_completeness": "not_verified_F2",
               "research_eligibility_completeness": "not_verified", "independent_cross_check": "not_performed"}
    path = directory / "acceptance-details.json"
    atomic_json(path, details)
    return {"acceptance_details_path": str(path), "board_acceptance": report}
