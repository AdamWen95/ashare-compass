"""Bounded daily price/volume observations, independent of company-body coverage.

Only a verified production selection may enter this workflow. Engineering runs
remain under their original entry points. Numbers come from the unchanged F4-S1
deterministic strategy; qualification is a separate evidence-backed decision.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import time
from uuid import uuid4

from .market_schemas import SHANGHAI
from .operations.daily import local_path
from .sector_benchmark import prepare_benchmark
from .sector_eligibility import collect_sector_eligibility, evaluate_sector_eligibility
from .sector_history import prepare_history, screening_history_inputs
from .sector_pipeline import write_new
from .sector_screening import evaluate_selection, screening_input_hash, validate_screening_config
from .sector_selection import digest, verify_selection
from .qualification_sources import collect_eligibility


def _now():
    return datetime.now(SHANGHAI).isoformat()


def prepare_observation_calendar(root, selection, config, *, max_seconds):
    """Fill only the missing trusted calendar windows, at most two SDK queries."""
    from .providers.baostock import BaoStockClient
    from .providers.baostock_f2 import resolve_history_calendar
    from .providers.sector_status import _permission
    _permission(root)
    began = time.monotonic()
    class BoundedCalendar(BaoStockClient):
        query_count = 0
        def query(self, operation, **parameters):
            remaining = max_seconds - (time.monotonic() - began)
            if operation != "calendar" or self.query_count >= 2 or remaining <= 0:
                raise ValueError("observation_calendar_budget_exhausted")
            self.query_count += 1
            self.timeout_seconds = min(15, remaining)
            return super().query(operation, **parameters)
    client = BoundedCalendar(timeout_seconds=min(15, max_seconds), max_attempts=1)
    result = resolve_history_calendar(date.fromisoformat(selection["target_date"]),
        local_path(root, config["calendar_cache"]), history_days=config.get("target_trading_days", 320),
        client=client, mode="research")
    result["network_requests"] = client.query_count
    return result


def _production(selection):
    verify_selection(selection)
    if (selection.get("purpose", "production") != "production" or selection.get("production_eligible") is False
            or selection["selection_id"].startswith("validation-") or selection.get("selection_verified") is not True):
        raise ValueError("daily observations require a verified production selection")


def _sealed(value):
    return {**value, "content_hash": digest(value)}


def combine_observations(selection, technical, eligibility):
    """Join both axes without treating unknown qualification as a passing result."""
    _production(selection)
    if (technical.get("result_hash") != digest({k: v for k, v in technical.items() if k != "result_hash"})
            or eligibility.get("content_hash") != digest({k: v for k, v in eligibility.items() if k != "content_hash"})
            or eligibility.get("technical_result_hash") != technical["result_hash"]):
        raise ValueError("observation input content or technical binding mismatch")
    for packet in (technical, eligibility):
        if any(packet.get(key) != selection.get(key) for key in ("selection_id", "target_date", "mode")) or packet.get("purpose") != "production":
            raise ValueError("observation input selection or provenance mismatch")
    expected = {member["security_id"] for member in selection["members"]}
    indexed = {}
    for packet in (technical, eligibility):
        rows = {row["security_id"]: row for row in packet["evaluations"]}
        if set(rows) != expected or len(rows) != len(packet["evaluations"]):
            raise ValueError("observation must retain every selected security exactly once")
        indexed[id(packet)] = rows
    qualifiers = indexed[id(eligibility)]
    rows, gaps = [], []
    for row in technical["evaluations"]:
        qualification = qualifiers[row["security_id"]]
        merged = deepcopy(row)
        merged.update(eligibility_status=qualification["eligibility_status"],
            eligibility_conditions=deepcopy(qualification["conditions"]),
            qualification_gaps=deepcopy(qualification["gaps"]),
            exclusion_reasons=deepcopy(qualification["exclusion_reasons"]),
            qualification_fact_hashes=[fact["fact_hash"] for fact in qualification["facts"]],
            company_review_status="not_reviewed", formal_verified_opportunity=False,
            observation_rank=None, candidate_rank=None,
            research_gaps=["公司公告、主营业务和财务尚未核查；未查到不表示没有风险。"],
            observation_label="量价条件与基本资格通过" if row["technical_status"] == "pass" and qualification["eligibility_status"] == "pass"
                else "技术条件达标、资格待核查" if row["technical_status"] == "pass" and qualification["eligibility_status"] == "pending"
                else "未进入量价观察名单")
        rows.append(merged)
        cache = row.get("cache_status", {})
        gap = {"security_id": row["security_id"], "symbol": row["symbol"],
            "missing_raw_dates": deepcopy(cache.get("missing_raw_dates", [])),
            "missing_adjusted_dates": deepcopy(cache.get("missing_adjusted_dates", [])),
            "strategy_inputs_ready": row.get("strategy_inputs_ready", False),
            "technical_status": row["technical_status"], "data_issues": deepcopy(row.get("data_issues", [])),
            "eligibility_status": qualification["eligibility_status"], "qualification_gaps": deepcopy(qualification["gaps"]),
            "historical_gap_status": "unexplained" if cache.get("missing_raw_dates") or cache.get("missing_adjusted_dates") else "none_recorded",
            "missing_dates_filled": False,
            "blocks_observation_eligibility": row["technical_status"] == "unknown"
                or row["technical_status"] == "pass" and qualification["eligibility_status"] == "pending"}
        if (gap["missing_raw_dates"] or gap["missing_adjusted_dates"] or gap["data_issues"]
                or gap["qualification_gaps"] or gap["blocks_observation_eligibility"]):
            gaps.append(gap)
    passed = sorted((row for row in rows if row["technical_status"] == "pass" and row["eligibility_status"] == "pass"),
        key=lambda row: (row["technical_rank"], row["symbol"], row["security_id"]))
    observations = passed[:technical["strategy_config"]["max_candidates"]]
    for rank, row in enumerate(observations, 1):
        row.update(observation_rank=rank, candidate_rank=rank)
    pending = sorted((row for row in rows if row["technical_status"] == "pass" and row["eligibility_status"] == "pending"),
        key=lambda row: (row["technical_rank"], row["symbol"], row["security_id"]))
    states = Counter(row["eligibility_status"] for row in rows)
    counts = {**technical["counts"], **{"eligibility_" + key + "_count": states[key] for key in ("pass", "fail", "pending")},
        "candidate_count": len(observations), "observation_count": len(observations), "qualified_technical_pass_count": len(passed),
        "observation_display_limit": technical["strategy_config"]["max_candidates"],
        "technical_pass_eligibility_pending_count": len(pending), "formal_verified_opportunity_count": 0,
        "data_gap_security_count": len(gaps), "blocking_gap_count": sum(row["blocks_observation_eligibility"] for row in gaps)}
    status = ("no_matching_sectors" if not rows else "observations_ready" if observations
        else "qualification_pending" if pending else "data_pending" if counts.get("technical_unknown_count", 0)
        else "no_matching_stocks")
    return {"status": status, "observations": observations, "pending": pending, "evaluations": rows, "counts": counts, "gaps": gaps}


def run_observation(root, selection, config, *, output_directory, online=True, max_seconds=1200,
                    strategy_config_path="config/sector_screening_f4s1.json",
                    eligibility_config_path="config/eligibility_sources.json", require_delisting_check=True):
    """Prepare selected history and return an immutable, complete daily evaluation.

    Online calls share a finite budget. Qualification calls are restricted to
    technical passes; all other members remain represented with dated evidence
    or explicit gaps. Offline invocation only reads caches, never fetches.
    """
    began = time.monotonic()
    if type(require_delisting_check) is not bool:
        raise ValueError("observation delisting policy must be boolean")
    if type(max_seconds) not in {int, float} or not math.isfinite(max_seconds) or not 0 < max_seconds <= 14400:
        raise ValueError("observation runtime budget must be positive and at most 14400 seconds")
    root = Path(root).resolve()
    _production(selection)
    if online and selection["mode"] != "research":
        raise ValueError("online observations reject offline test data")
    base = local_path(root, str(output_directory))
    if selection["mode"] != "research" and "research" in {part.casefold() for part in base.parts}:
        raise ValueError("offline observations cannot write production research artifacts")
    directory = base / ("observation-" + datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8])
    strategy = validate_screening_config(json.loads(local_path(root, strategy_config_path).read_text(encoding="utf-8-sig")))
    def remaining(cap=None):
        value = max(0, max_seconds - (time.monotonic() - began))
        return min(value, cap) if cap is not None else value
    history_report = {"status": "not_applicable", "metrics": {"network_requests": 0}}
    history = {"calendar": {"verified": True, "trading_dates": [], "issues": []}, "securities": {}, "issues": [], "file_refs": []}
    benchmark = {"status": "not_applicable", "records": [], "issues": [], "file_refs": [], "network_requests": 0}
    calendar_preparation = {"status": "not_requested", "network_requests": 0}
    if selection["members"]:
        if online and remaining() > 0:
            calendar_preparation = prepare_observation_calendar(root, selection, config, max_seconds=remaining(30))
            write_new(directory / "calendar_preparation.json", calendar_preparation)
        # Reserve time for one benchmark and dated qualification of technical
        # passes. A large S does not cause a daily full-market history fetch.
        history_report = prepare_history(root, selection, config, max_seconds=remaining(max_seconds * .65) if online else 0)
        history = screening_history_inputs(root, selection, config)
        if history.get("calendar", {}).get("verified"):
            budget = remaining(30)
            benchmark = prepare_benchmark(root, selection, config, online=online and budget > 0, max_seconds=max(.001, budget))
        else:
            benchmark = {"status": "calendar_pending", "records": [], "issues": ["benchmark_calendar_unverified"], "file_refs": [], "network_requests": 0}
    cutoff = _now()
    implementation = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in ("sector_observation.py", "sector_screening.py", "sector_eligibility.py", "sector_history.py", "factors/trend.py")}
    inputs = {"schema_version": "f3s-screening-input-v1", "mode": selection["mode"], "purpose": "production",
        "selection_id": selection["selection_id"], "selection_content_hash": selection["content_hash"], "target_date": selection["target_date"],
        "cutoff_at": cutoff, "calendar": history["calendar"], "securities": history["securities"], "benchmark": benchmark,
        "file_refs": history.get("file_refs", []) + benchmark.get("file_refs", []), "history_issues": history.get("issues", []),
        "implementation_hashes": implementation, "source_cutoff_at": selection["cutoff_at"],
        "historical_reconstruction": cutoff[:10] > selection["target_date"]}
    inputs["input_hash"] = screening_input_hash(inputs)
    technical = evaluate_selection(selection, inputs, strategy)
    probe_ids = [row["security_id"] for row in sorted(
        (row for row in technical["evaluations"] if row["technical_status"] == "pass"),
        key=lambda row: (row["technical_rank"], row["symbol"], row["security_id"]))]
    exchange = {"status": "not_requested" if require_delisting_check else "not_required_by_policy",
        "source_health": [], "bundle_file": None}
    budget = remaining(60)
    if require_delisting_check and probe_ids and online and budget > 0:
        exchange = collect_eligibility(config_path=local_path(root, eligibility_config_path),
            target=date.fromisoformat(selection["target_date"]), output_dir=directory / "exchange_eligibility", max_seconds=budget)
    # Supplying an absent explicit path prevents historical engineering acceptance
    # bundles from becoming an implicit daily source.
    bundle = exchange.get("bundle_file") or directory / "no_current_exchange_bundle.json"
    budget = remaining()
    facts = collect_sector_eligibility(root, selection, output_directory=directory / "qualification",
        online=online and budget > 0 and bool(probe_ids), max_seconds=max(.001, budget), legacy_bundle=bundle,
        max_probe_members=max(1, len(selection["members"])), probe_security_ids=probe_ids)
    eligibility = evaluate_sector_eligibility(selection, technical, facts, cutoff_at=_now(),
        require_delisting_check=require_delisting_check)
    joined = combine_observations(selection, technical, eligibility)
    files = {"selection.json": write_new(directory / "selection.json", selection),
        "screening_inputs.json": write_new(directory / "screening_inputs.json", inputs),
        "strategy_config.json": write_new(directory / "strategy_config.json", strategy),
        "technical_evaluation.json": write_new(directory / "technical_evaluation.json", technical),
        "eligibility_evaluation.json": write_new(directory / "eligibility_evaluation.json", eligibility),
        "history_readiness.json": write_new(directory / "history_readiness.json", history_report),
        "calendar_preparation_summary.json": write_new(directory / "calendar_preparation_summary.json", calendar_preparation),
        "exchange_eligibility_result.json": write_new(directory / "exchange_eligibility_result.json", exchange)}
    result = _sealed({"schema_version": "sector-observation-v1", "mode": selection["mode"], "purpose": "production",
        "production_eligible": selection["mode"] == "research", "selection_id": selection["selection_id"],
        "selection_content_hash": selection["content_hash"], "target_date": selection["target_date"],
        "cutoff_at": eligibility["cutoff_at"], "source_cutoff_at": selection["cutoff_at"],
        "eligibility_policy": deepcopy(eligibility["eligibility_policy"]),
        "historical_reconstruction": eligibility["historical_reconstruction"], **joined,
        "technical": technical, "eligibility": eligibility, "evidence_directory": str(directory),
        "history": history_report, "exchange_eligibility": exchange, "field_facts_file": facts.file_ref,
        "model_calls": 0, "model_tokens": 0, "network_requests": history_report.get("metrics", {}).get("network_requests", 0)
            + calendar_preparation["network_requests"] + benchmark.get("network_requests", 0) + facts.get("network_requests", 0)
            + sum(len(source.get("requests", [])) for source in exchange.get("source_health", [])),
        "runtime_limit_seconds": max_seconds, "elapsed_seconds": round(time.monotonic() - began, 6),
        "qualification_probe_security_ids": probe_ids,
        "limitations": ["仅对冻结关注行业内股票筛选，不覆盖行业以外机会。", "公司材料缺失不阻塞量价筛选；公告、主营及财务尚未核查。",
            "基本资格未知的技术达标股票单列待核查，不能计入通过名单。", "历史缺日不补价、不缩短120个交易日窗口；预算不足保留缺口。",
            "量价观察不是未来收益预测或交易指令。"]})
    files["observation.json"] = write_new(directory / "observation.json", result)
    write_new(directory / "manifest.json", _sealed({"schema_version": "sector-observation-manifest-v1",
        "selection_id": selection["selection_id"], "selection_content_hash": selection["content_hash"],
        "mode": selection["mode"], "purpose": "production", "files": files, "field_facts_file": facts.file_ref,
        "observation_content_hash": result["content_hash"]}))
    return result
