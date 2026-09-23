"""Deterministic, evidenced sector observation; never individual screening."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
from statistics import median

from .providers.base import BOARD_EXCHANGES
from .market_schemas import SHANGHAI

RULE_VERSION = "sector_daily_strength_v1"
DEFAULT_PARAMETERS = {"preselect_limit": 6, "selected_limit": 3,
    "source_change_min_exclusive": 0, "source_amount_min_exclusive": 0,
    "median_min_exclusive": 0, "advancing_fraction_min": 0.50}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("missing_or_invalid_number")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid_number") from exc
    if not parsed.is_finite():
        raise ValueError("nonfinite_number")
    return parsed


def parameters(values=None):
    result = {**DEFAULT_PARAMETERS, **(values or {})}
    if set(result) != set(DEFAULT_PARAMETERS):
        raise ValueError("unknown_rule_parameter")
    if (type(result["preselect_limit"]) is not int or not 1 <= result["preselect_limit"] <= 6
            or type(result["selected_limit"]) is not int or not 1 <= result["selected_limit"] <= 3
            or result["selected_limit"] > result["preselect_limit"]):
        raise ValueError("industry_limits_invalid")
    # Changing the rule means a new version; v1 cannot silently weaken gates.
    for key in set(DEFAULT_PARAMETERS) - {"preselect_limit", "selected_limit"}:
        if number(result[key]) < number(DEFAULT_PARAMETERS[key]):
            raise ValueError("rule_gate_cannot_be_lowered")
    return result


def symbol_of(member):
    exchange = member.get("exchange")
    return {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(exchange, "?") + member.get("code", "")


def confirmed_full_day_halt(member, target):
    halt = member.get("statuses", {}).get("suspended", {})
    return (halt.get("value") is True and halt.get("verified") is True and halt.get("full_day") is True
        and bool(halt.get("source")) and bool(halt.get("evidence_id"))
        and halt.get("as_of_date") == target
        and (halt.get("effective_from") or target) <= target <= (halt.get("effective_to") or target))


def quote_issues(row, target):
    issues = list(row.get("issues", []))
    if row.get("trade_date") != target:
        issues.append("quote_date_missing_or_mismatch")
    try:
        stamp = datetime.fromisoformat(row["quote_at"])
        if stamp.utcoffset() is None or stamp.astimezone(SHANGHAI).date().isoformat() != target:
            raise ValueError()
        if stamp.astimezone(SHANGHAI).time() < time(15):
            issues.append("intraday_quote")
    except (ValueError, TypeError, KeyError):
        issues.append("dated_after_close_evidence_missing")
    try:
        change = number(row.get("change_pct"))
        amount = number(row.get("amount_cny"))
        close = number(row.get("close"))
        reference = number(row.get("reference_price"))
        if amount < 0 or close <= 0 or reference <= 0:
            issues.append("invalid_price_or_amount")
        if row.get("percentage_basis") not in {"source_reference_price", "source_daily_percentage"}:
            issues.append("percentage_basis_unverified")
        if row.get("amount_unit") != "CNY" or row.get("price_unit") != "CNY/share":
            issues.append("quote_units_unverified")
        if abs(change - (close / reference - 1) * 100) > Decimal("0.03"):
            issues.append("change_reference_conflict")
    except ValueError:
        issues.append("quote_numeric_fields_missing")
    return sorted(set(issues))


def map_members(response, universe, sector):
    """Supplier symbols locate evidenced master records; prefixes do not type securities."""
    lookup = {}
    for member in universe.get("members", []):
        lookup.setdefault(symbol_of(member), []).append(member)
    rows, mapped, issues, seen = [], {}, [], set()
    for raw in response.get("rows", []):
        symbol = str(raw.get("symbol", ""))
        row = {"sector_id": sector["sector_id"], "taxonomy": sector["taxonomy"],
            "sector_name": sector["name"], "symbol": symbol, "raw": deepcopy(raw),
            "security_id": None, "mapping_status": "unknown_identity", "reason": "not_in_verified_master"}
        matches = lookup.get(symbol, [])
        if symbol in seen:
            row.update(mapping_status="duplicate", reason="duplicate_source_member")
            issues.append("duplicate_source_member")
        elif len(matches) > 1:
            row.update(mapping_status="conflict", reason="master_identity_conflict")
            issues.append("master_identity_conflict")
        elif len(matches) == 1:
            member = matches[0]
            row.update(security_id=member.get("security_id"), code=member.get("code"),
                       exchange=member.get("exchange"), listing_board=member.get("board"),
                       security_type=member.get("security_type"), metadata_source=member.get("metadata_source"))
            if not member.get("metadata_verified") or member.get("metadata_conflict"):
                row.update(mapping_status="conflict", reason="unverified_master_metadata")
                issues.append(row["reason"])
            elif member.get("security_type") != "ordinary_a":
                row.update(mapping_status="excluded", reason="source_verified_non_ordinary_a")
            elif member.get("board") not in BOARD_EXCHANGES:
                row.update(mapping_status="excluded", reason="source_verified_outside_market_scope")
            elif raw.get("security_id") not in (None, member["security_id"]):
                row.update(mapping_status="conflict", reason="supplier_identity_conflict")
                issues.append(row["reason"])
            else:
                row.update(mapping_status="included", reason="matched_verified_master")
                mapped[member["security_id"]] = member
        elif raw.get("metadata_verified") is True and raw.get("metadata_evidence") and (
                raw.get("security_type") in {"b_share", "cdr", "fund", "index"}
                or raw.get("exchange") == "BSE"):
            row.update(mapping_status="excluded", reason="source_verified_outside_market_scope")
        else:
            issues.append("unknown_identity")
        seen.add(symbol)
        rows.append(row)
    if response.get("complete") is not True:
        issues.append("membership_end_unverified")
    if not response.get("boundary_verified"):
        issues.append("membership_boundary_evidence_missing")
    if response.get("issues"):
        issues.extend(response["issues"])
    return rows, mapped, sorted(set(issues))


def evaluate(universe, catalog, memberships, quotes, *, target, cutoff, config, calendar, mode="research"):
    """Pure inputs only. The network/persistence boundary validates real provenance."""
    params = parameters(config.get("parameters"))
    if mode not in {"research", "offline_test"}:
        raise ValueError("sector_mode_invalid")
    cutoff_time = datetime.fromisoformat(cutoff)
    if cutoff_time.utcoffset() is None:
        raise ValueError("cutoff_requires_timezone")
    taxonomy = config["taxonomy"]
    blockers = []
    if (universe.get("scope") != "sse_szse_a" or not universe.get("universe_verified")
            or not universe.get("collection_ready")):
        blockers.append("universe_unverified")
    if calendar.get("calendar_verified") is not True or calendar.get("calendar", {}).get(target) is not True:
        blockers.append("calendar_unverified")
    if mode == "research":
        if universe.get("mode") != "research":
            blockers.append("test_universe_rejected")
        for packet in (catalog, quotes, *memberships.values()):
            if packet.get("provenance_mode") != "online":
                blockers.append("source_provenance_unverified")
    if catalog.get("complete") is not True or not catalog.get("boundary_verified"):
        blockers.append("catalog_incomplete")
    if catalog.get("status") not in {"ok", "complete"}:
        blockers.append("sector_source_unavailable")
    quote_map = {}
    for row in quotes.get("rows", []):
        quote_map.setdefault(row.get("symbol"), []).append(row)
    sectors, all_memberships, ids = [], [], set()
    for source in catalog.get("rows", []):
        row = deepcopy(source)
        sector_id = row.get("sector_id")
        row.update(pre_rank=None, selected_rank=None, selected=False, reasons=[], membership_complete=False)
        if (row.get("taxonomy") != taxonomy or row.get("kind") != "industry"
                or not isinstance(sector_id, str) or not sector_id):
            row["reasons"].append("taxonomy_or_kind_mismatch")
        if sector_id in ids:
            row["reasons"].append("duplicate_sector_id")
            blockers.append("catalog_identity_conflict")
        ids.add(sector_id)
        response = memberships.get(sector_id, {})
        mapped_rows, mapped, mapping_issues = map_members(response, universe, row)
        all_memberships.extend(mapped_rows)
        member_quotes, missing, halted = [], [], []
        for security_id, member in sorted(mapped.items()):
            symbol = symbol_of(member)
            values = quote_map.get(symbol, [])
            # Only dated authoritative evidence can remove a full-day halt from Q.
            extra = values[0].get("full_day_halt_evidence") if len(values) == 1 else None
            supplemented = {"statuses": {"suspended": extra}} if isinstance(extra, dict) else member
            if confirmed_full_day_halt(member, target) or confirmed_full_day_halt(supplemented, target):
                halted.append(security_id)
                continue
            problems = quote_issues(values[0], target) if len(values) == 1 else ["quote_missing_or_duplicate"]
            if not problems and datetime.fromisoformat(values[0]["quote_at"]) > cutoff_time:
                problems.append("quote_after_selection_cutoff")
            if problems:
                missing.append({"security_id": security_id, "symbol": symbol, "issues": problems})
            else:
                member_quotes.append(values[0])
        row.update(raw_member_count=len(mapped_rows), scope_member_count=len(mapped),
            excluded_count=sum(r["mapping_status"] == "excluded" for r in mapped_rows),
            identity_issue_count=sum(r["mapping_status"] in {"unknown_identity", "conflict", "duplicate"} for r in mapped_rows),
            membership_issues=mapping_issues, membership_complete=not mapping_issues,
            expected_quote_count=len(mapped)-len(halted), valid_quote_count=len(member_quotes),
            full_day_halted=halted, missing_quotes=missing, quote_complete=not missing and bool(mapped),
            source_displayed_count=response.get("displayed_count"),
            source_count_discrepancy=response.get("count_discrepancy"),
            member_security_ids=sorted(mapped))
        row["comparison_not_applicable"] = not mapped and not mapping_issues
        changes = [number(q["change_pct"]) for q in member_quotes]
        amount = sum((number(q["amount_cny"]) for q in member_quotes), Decimal(0))
        denominator = row["expected_quote_count"]
        row.update(median_change_pct=float(median(changes)) if changes else None,
            advancing_count=sum(c > 0 for c in changes), flat_count=sum(c == 0 for c in changes),
            declining_count=sum(c < 0 for c in changes),
            advancing_fraction=float(Decimal(sum(c > 0 for c in changes))/denominator) if denominator else None,
            total_amount_cny=float(amount), mean_amount_cny=float(amount/denominator) if denominator else None)
        # v1 fallback is the arithmetic mean of complete, dated ordinary-A members.
        # The undated supplier statistic is retained above, never assigned target T.
        if not mapping_issues and not missing and changes:
            row.update(ranking_change_pct=float(sum(changes)/len(changes)), ranking_amount_cny=float(amount),
                ranking_basis="member_mean_daily_change_v1", ranking_business_date=target,
                ranking_date_verified=True)
        else:
            row.update(ranking_change_pct=None, ranking_amount_cny=None, ranking_date_verified=False)
            row["reasons"].append("no_in_scope_members" if row["comparison_not_applicable"] else "complete_dated_members_required_for_aggregate")
        sectors.append(row)
    industry_count = sum(s.get("kind") == "industry" and s.get("taxonomy") == taxonomy for s in sectors)
    comparison_count = sum(s.get("kind") == "industry" and s.get("taxonomy") == taxonomy
                           and not s["comparison_not_applicable"] for s in sectors)
    rankable = [s for s in sectors if s["ranking_date_verified"] and not s["reasons"]]
    positive = [s for s in rankable if s["ranking_change_pct"] > params["source_change_min_exclusive"]
                and s["ranking_amount_cny"] > params["source_amount_min_exclusive"]]
    positive.sort(key=lambda s: (-s["ranking_change_pct"], s["sector_id"]))
    pre = positive[:params["preselect_limit"]]
    for i, row in enumerate(pre, 1):
        row["pre_rank"] = i
    eligible = [s for s in pre if s["median_change_pct"] > params["median_min_exclusive"]
                and s["advancing_fraction"] >= params["advancing_fraction_min"]]
    eligible.sort(key=lambda s: (-s["median_change_pct"], -s["advancing_fraction"], s["sector_id"]))
    final = eligible[:params["selected_limit"]] if not blockers else []
    for i, row in enumerate(final, 1):
        row.update(selected=True, selected_rank=i)
    for row in sectors:
        if row["selected"]:
            row["reasons"].append("passed_daily_observation_rule")
        elif row in eligible:
            row["reasons"].append("outside_selected_rank_limit" if not blockers else "selection_inputs_blocked")
        elif row in pre:
            row["reasons"].append("median_or_breadth_below_gate")
        elif row in positive:
            row["reasons"].append("outside_preselection_rank_limit")
        elif row in rankable:
            row["reasons"].append("nonpositive_daily_change_or_amount")
    if not rankable and not blockers:
        blockers.append("sector_date_or_membership_unverified")
    if not final and len(rankable) < comparison_count and not blockers:
        blockers.append("incomplete_comparison_cannot_claim_zero")
    member_lookup = {m["security_id"]: m for m in universe.get("members", [])}
    association = {}
    for row in final:
        for security_id in row["member_security_ids"]:
            association.setdefault(security_id, []).append(row["sector_id"])
    members = [{**deepcopy(member_lookup[key]), "sector_ids": sorted(value)} for key, value in sorted(association.items())]
    for member in members:
        values = quote_map.get(symbol_of(member), [])
        evidence = values[0].get("full_day_halt_evidence") if len(values) == 1 else None
        if isinstance(evidence, dict) and confirmed_full_day_halt({"statuses": {"suspended": evidence}}, target):
            member["supplemental_status_evidence"] = {"suspended": deepcopy(evidence)}
    status = "selected" if final else "selection_blocked" if blockers else "no_matching_sectors"
    classified_ids = {r["security_id"] for r in all_memberships if r["mapping_status"] == "included"}
    payload = {"schema_version": "f2s1-selection-v1", "mode": mode, "market_scope": "sse_szse_a",
        "research_mode": "sector_first", "title": "沪深 A 股·板块精选研究", "target_date": target,
        "cutoff_at": cutoff, "taxonomy": taxonomy, "themes_enabled": False,
        "rule_version": RULE_VERSION, "parameters": params, "config_hash": digest(config),
        "selection_status": status, "selection_verified": not blockers,
        "blockers": sorted(set(blockers)), "universe_snapshot_id": universe.get("snapshot_id"),
        "universe_content_hash": universe.get("content_hash"), "universe_count": universe.get("ordinary_a_count"),
        "universe_board_counts": universe.get("board_counts"), "universe_observed_at": universe.get("observed_at"),
        "universe_source_business_dates": [{"provider": m.get("provider"),
            "exchange": m.get("exchange"), "source_business_date": m.get("source_business_date")}
            for m in universe.get("source_manifests", [])],
        "historical_reconstruction": True, "time_basis": "observed_membership_with_dated_close_backfill",
        "scope_notice": "暂不含北交所；不覆盖关注板块以外的个股机会；当前观察成分不冒充历史当时成分",
        "catalog_count": industry_count, "raw_catalog_count": len(sectors), "valid_ranking_count": len(rankable),
        "industry_comparison_complete": len(rankable) == comparison_count and catalog.get("complete") is True,
        "comparison_industry_count": comparison_count,
        "preselected_count": len(pre), "selected_count": len(final), "selected_sectors": final,
        "sectors": sectors, "members": members, "selected_security_count": len(members),
        "raw_membership_count": len(all_memberships),
        "catalog_mapped_security_count": len(classified_ids),
        "unassigned_universe_count": universe.get("ordinary_a_count", 0)-len(classified_ids),
        "selected_raw_membership_count": sum(s["raw_member_count"] for s in final),
        "out_of_selection_history_policy": "not_requested_by_design", "model_calls": 0,
        "research_thresholds": {"valid_history_days": 120, "mean_amount_20_cny": 50000000},
        "evidence_hashes": {"catalog": digest(catalog), "memberships": digest(memberships),
            "quotes": digest(quotes), "calendar": digest(calendar)}}
    payload["content_hash"] = digest(payload)
    payload["selection_id"] = "sector-" + target + "-" + payload["content_hash"][:20]
    return payload, all_memberships


def verify_selection(value):
    unhashed = {k: v for k, v in value.items() if k not in {"selection_id", "content_hash"}}
    hashed = digest(unhashed)
    validation = value.get("purpose") == "engineering_validation"
    if validation and (value.get("production_eligible") is not False
            or value.get("schema_version") != "f3s-validation-selection-v1"
            or not value.get("source_selection_id") or value.get("automatic_selection") is not False):
        raise ValueError("validation_selection_purpose_invalid")
    prefix = "validation-sector-" if validation else "sector-"
    if value.get("purpose", "production") not in {"production", "engineering_validation"}:
        raise ValueError("unknown_selection_purpose")
    if value.get("content_hash") != hashed or value.get("selection_id") != prefix + value["target_date"] + "-" + hashed[:20]:
        raise ValueError("frozen_selection_hash_mismatch")
    if value.get("market_scope") != "sse_szse_a" or value.get("research_mode") != "sector_first":
        raise ValueError("frozen_selection_scope_mismatch")
    members = value.get("members", [])
    if len(members) != len({m["security_id"] for m in members}) or len(members) != value["selected_security_count"]:
        raise ValueError("frozen_selection_duplicate_or_truncated")
    return value
