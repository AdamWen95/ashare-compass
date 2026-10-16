"""Bounded relative-strength research pool, independent of market posture."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from statistics import median

from ..providers.base import BOARD_EXCHANGES, SecurityIdentity
from ..sector_selection import (
    RULE_VERSION as STRICT_RULE_VERSION, confirmed_full_day_halt, digest, evaluate,
    number, quote_issues, symbol_of, verify_selection,
)
from ..universe import KNOWN_OTHER_TYPES
from .contracts import SHANGHAI


RULE_VERSION = "premarket-relative-observation-v1"
PARAMETERS = {"sector_limit": 3, "min_sector_members": 5, "members_per_sector": 5,
    "max_history_securities": 15, "stock_min_amount_cny": 50000000}


def _verify_binding(selection, packet):
    verify_selection(selection)
    if (selection.get("rule_version") != STRICT_RULE_VERSION or selection.get("observation_only")
            or selection.get("purpose", "production") != "production"):
        raise ValueError("relative_strict_selection_binding_invalid")
    config_hash = selection.get("config_hash")
    if (not isinstance(config_hash, str) or len(config_hash) != 64
            or any(char not in "0123456789abcdef" for char in config_hash)):
        raise ValueError("relative_config_hash_missing_or_invalid")
    for key in ("catalog", "memberships", "quotes", "calendar"):
        if key not in packet or digest(packet[key]) != selection.get("evidence_hashes", {}).get(key):
            raise ValueError("relative_source_hash_binding_mismatch:" + key)
    universe = packet["universe"]
    try:
        universe_date = date.fromisoformat(universe["requested_date"])
        market_date = date.fromisoformat(selection["target_date"])
        cutoff_date = datetime.fromisoformat(selection["cutoff_at"]).astimezone(SHANGHAI).date()
        if not market_date <= universe_date <= cutoff_date:
            raise ValueError("out_of_window")
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("relative_universe_date_binding_mismatch") from exc
    body = {k: v for k, v in universe.items() if k not in {"snapshot_id", "content_hash"}}
    hashed = digest(body)
    expected_id = "universe-" + str(universe.get("requested_date")) + "-" + hashed[:20]
    if (universe.get("content_hash") != hashed or universe.get("snapshot_id") != expected_id
            or universe.get("content_hash") != selection.get("universe_content_hash")
            or universe.get("snapshot_id") != selection.get("universe_snapshot_id")):
        raise ValueError("relative_universe_hash_binding_mismatch")
    mode = selection.get("mode")
    provenance = "online" if mode == "research" else "offline_test"
    if mode not in {"research", "offline_test"} or universe.get("mode") != mode or any(
            source.get("provenance_mode") != provenance for source in
            (packet["catalog"], packet["quotes"], *packet["memberships"].values())):
        raise ValueError("relative_source_provenance_unverified")
    # Replaying the existing pure evaluator binds comparison summaries as well
    # as input hashes. A self-rehashed, truncated summary is not complete proof.
    replay, _ = evaluate(universe, packet["catalog"], packet["memberships"], packet["quotes"],
        target=selection["target_date"], cutoff=selection["cutoff_at"],
        config={"taxonomy": selection["taxonomy"], "parameters": selection["parameters"]},
        calendar=packet["calendar"], mode=mode)
    ignored = {"config_hash", "content_hash", "selection_id"}
    if any(selection.get(key) != value for key, value in replay.items() if key not in ignored):
        raise ValueError("relative_comparison_packet_binding_mismatch")


def _ordinary_members(universe):
    members, identities, symbols = {}, set(), set()
    for member in universe.get("members", []):
        identity, symbol = member.get("security_id"), symbol_of(member)
        if not identity or identity in identities or symbol in symbols:
            raise ValueError("relative_universe_identity_conflict")
        identities.add(identity)
        symbols.add(symbol)
        if member.get("metadata_verified") is not True or member.get("metadata_conflict"):
            raise ValueError("relative_universe_identity_unverified")
        if member.get("security_type") in KNOWN_OTHER_TYPES:
            continue
        if member.get("discovery_classification") in {"not_yet_listed", "delisted_with_evidence"}:
            continue
        if (member.get("security_type") != "ordinary_a"
                or BOARD_EXCHANGES.get(member.get("board")) != member.get("exchange")):
            raise ValueError("relative_universe_identity_scope_invalid")
        try:
            SecurityIdentity(**{k: member[k] for k in (
                "security_id", "code", "exchange", "board", "security_type", "metadata_verified")})
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("relative_universe_identity_invalid") from exc
        members[identity] = member
    if not members or len(members) != universe.get("ordinary_a_count"):
        raise ValueError("relative_universe_identity_denominator_invalid")
    return members


def _market_quotes(selection, packet, members):
    lookup = defaultdict(list)
    for quote in packet["quotes"].get("rows", []):
        lookup[quote.get("symbol")].append(quote)
    valid, halted, missing = {}, [], []
    target, cutoff = selection["target_date"], datetime.fromisoformat(selection["cutoff_at"])
    for identity, member in sorted(members.items()):
        rows = lookup[symbol_of(member)]
        if len(rows) > 1:
            raise ValueError("relative_duplicate_quote_identity")
        quote = rows[0] if len(rows) == 1 else None
        if quote is not None:
            if quote.get("security_id") != identity:
                raise ValueError("relative_quote_identity_binding_mismatch")
            quoted_identity = quote.get("identity", {})
            if any(quoted_identity.get(k) != member.get(k) for k in (
                    "security_id", "code", "exchange", "board", "security_type", "metadata_verified")):
                raise ValueError("relative_quote_identity_binding_mismatch")
        overlay = quote.get("full_day_halt_evidence") if quote and quote.get("trade_date") == target else None
        supplemented = {"statuses": {"suspended": overlay}} if isinstance(overlay, dict) else {}
        if confirmed_full_day_halt(member, target) or confirmed_full_day_halt(supplemented, target):
            halted.append(identity)
        elif quote is None or quote_issues(quote, target) or datetime.fromisoformat(quote["quote_at"]) > cutoff:
            missing.append(identity)
        else:
            valid[identity] = quote
    complete = not missing and bool(valid)
    changes = [number(row["change_pct"]) for row in valid.values()]
    mean = sum(changes) / len(changes) if complete else None
    advancing = Decimal(sum(value > 0 for value in changes)) / len(changes) if complete else None
    baseline = {"status": "ready" if complete else "unavailable", "market_as_of_date": target,
        "scope": "sse_szse_a", "basis": "equal_weight_verified_ordinary_a_daily_change",
        "sample_count": len(valid), "halted_count": len(halted), "missing_count": len(missing),
        "missing_ids": missing, "mean_change_pct": float(mean) if mean is not None else None,
        "advancing_fraction": float(advancing) if advancing is not None else None}
    return valid, baseline, mean, advancing


def _directions(selection, quotes, market_mean, market_advancing):
    rows, ranked = deepcopy(selection["sectors"]), []
    for row in rows:
        row.update(selected=False, selected_rank=None, pre_rank=None, reasons=[])
        if (row.get("taxonomy") != selection["taxonomy"] or row.get("kind") != "industry"
                or row.get("comparison_not_applicable")):
            row["reasons"] = ["outside_relative_industry_scope"]
            continue
        values = [quotes[i] for i in row["member_security_ids"] if i in quotes]
        changes = [number(quote["change_pct"]) for quote in values]
        if not changes:
            row["reasons"] = ["relative_sample_unavailable"]
            continue
        mean = sum(changes) / len(changes)
        advancing = Decimal(sum(value > 0 for value in changes)) / len(changes)
        amount = sum((number(quote["amount_cny"]) for quote in values), Decimal(0))
        row.update(direction_kind="relative_strength", relative_metrics={
            "mean_change_pct": float(mean), "median_change_pct": float(median(changes)),
            "advancing_fraction": float(advancing), "market_mean_change_pct": float(market_mean),
            "market_advancing_fraction": float(market_advancing), "excess_market_pp": float(mean - market_mean),
            "absolute_up": mean > 0, "member_count": len(changes)})
        if (len(changes) < PARAMETERS["min_sector_members"] or mean <= market_mean
                or advancing < market_advancing or amount <= 0):
            row["reasons"] = ["relative_sample_strength_breadth_or_amount_below_gate"]
        else:
            ranked.append((row, mean, advancing))
    ranked.sort(key=lambda item: (-(item[1] - market_mean), -item[2], item[0]["sector_id"]))
    selected = []
    for rank, (row, _, _) in enumerate(ranked, 1):
        row["pre_rank"] = rank
        if rank <= PARAMETERS["sector_limit"]:
            row.update(selected=True, selected_rank=rank, reasons=["passed_relative_observation_rule"])
            selected.append(row)
        else:
            row["reasons"] = ["outside_relative_direction_limit"]
    return rows, selected, len(ranked)


def _pool(directions, members, quotes, market_mean, target):
    selected, eligible_ids = {}, set()
    for sector in directions:
        values = [quotes[i] for i in sector["member_security_ids"] if i in quotes]
        sector_mean = sum(number(quote["change_pct"]) for quote in values) / len(values)
        eligible = []
        for identity in sector["member_security_ids"]:
            if identity not in quotes or identity not in members:
                continue
            member, quote = members[identity], quotes[identity]
            if (member.get("listing_status") == "delisted" or any(
                    member.get("statuses", {}).get(key, {}).get("value") is True for key in ("st", "suspended"))
                    or quote.get("is_st") in (True, "1") or quote.get("tradestatus") in (0, "0")):
                continue
            change, amount = number(quote["change_pct"]), number(quote["amount_cny"])
            if change <= sector_mean or change <= market_mean or amount < PARAMETERS["stock_min_amount_cny"]:
                continue
            relative = {"sector_id": sector["sector_id"], "sector_mean_change_pct": float(sector_mean),
                "market_mean_change_pct": float(market_mean), "stock_change_pct": float(change),
                "excess_market_pp": float(change - market_mean), "excess_sector_pp": float(change - sector_mean),
                "amount_cny": float(amount), "market_as_of_date": target, "kind": "single_session_relative_strength"}
            eligible.append((identity, change, amount, relative))
        eligible.sort(key=lambda item: (-item[1], -item[2], item[0]))
        eligible_ids.update(item[0] for item in eligible)
        for identity, _, _, relative in eligible[:PARAMETERS["members_per_sector"]]:
            if identity in selected:
                selected[identity]["sector_ids"].append(sector["sector_id"])
            else:
                selected[identity] = {**deepcopy(members[identity]), "sector_ids": [sector["sector_id"]],
                    "relative_observation": relative}
    return [selected[key] for key in sorted(selected)], len(eligible_ids)


def build_observation_selection(strict_selection, packet):
    """Derive a frozen <=15-stock pool; leave source facts and strict rules intact.

    Conflicting bindings raise; incomplete but honestly frozen inputs yield an
    explicitly blocked empty pool. No source calls or qualification claims occur.
    """
    _verify_binding(strict_selection, packet)
    members = _ordinary_members(packet["universe"])
    quotes, baseline, market_mean, market_advancing = _market_quotes(strict_selection, packet, members)
    blockers = list(strict_selection.get("blockers", []))
    if strict_selection.get("selection_verified") is not True:
        blockers.append("relative_strict_inputs_unverified")
    if strict_selection.get("industry_comparison_complete") is not True:
        blockers.append("relative_industry_comparison_incomplete")
    if baseline["status"] != "ready":
        blockers.append("relative_market_quotes_incomplete")
    sectors, directions, preselected, pool, eligible_count = [], [], 0, [], 0
    if not blockers:
        sectors, directions, preselected = _directions(strict_selection, quotes, market_mean, market_advancing)
        pool, eligible_count = _pool(directions, members, quotes, market_mean, strict_selection["target_date"])
    else:
        sectors = deepcopy(strict_selection["sectors"])
        for sector in sectors:
            sector.update(selected=False, selected_rank=None, pre_rank=None,
                          reasons=["relative_selection_inputs_blocked"])
    result = deepcopy(strict_selection)
    result.update(rule_version=RULE_VERSION, parameters=deepcopy(PARAMETERS), observation_only=True,
        basis_selection_id=strict_selection["selection_id"], basis_selection_content_hash=strict_selection["content_hash"],
        title="沪深 A 股·相对强势观察", selection_verified=not blockers, blockers=sorted(set(blockers)),
        selection_status="selection_blocked" if blockers else "selected" if directions else "no_matching_sectors",
        sectors=sectors, selected_sectors=directions, selected_count=len(directions), preselected_count=preselected,
        members=pool, selected_security_count=len(pool), relative_market_baseline=baseline,
        selected_raw_membership_count=sum(s["raw_member_count"] for s in directions),
        scope_notice="暂不含北交所；仅对最多3个相对强势方向、每方向最多5只股票补充历史与资格核查；"
            "单日相对强势可为相对抗跌，不代表上涨趋势或收益预测；池外个股未深入分析。",
        observation_scope={**PARAMETERS, "eligible_security_count": eligible_count,
            "selected_security_count": len(pool), "outside_pool_history": "not_requested_by_design",
            "qualification_status": "not_evaluated", "full_market_deep_analysis": False})
    result.pop("content_hash", None)
    result.pop("selection_id", None)
    result["content_hash"] = digest(result)
    result["selection_id"] = "sector-" + result["target_date"] + "-" + result["content_hash"][:20]
    return verify_selection(result)
