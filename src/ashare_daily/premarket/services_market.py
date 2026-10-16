"""Production morning market adapter over the existing source boundaries."""
from __future__ import annotations

from datetime import date, datetime
from copy import deepcopy
import time

from ..operations.daily import atomic_json, local_path
from ..operations.observation import load_options, supplement_zero_quotes, _observe, _reference_review
from ..provider_diagnostics import _read_snapshot
from ..sector_pipeline import load_config, collect_inputs, archive_selection
from ..sector_selection import evaluate
from ..sector_benchmark import prepare_benchmark
from ..reference_strategy import load_reference_inputs
from ..reports.observation import build_report
from ..universe_service import sync_date, load_universe_config
from ..financial_review import collect_financial_review
from .contracts import SHANGHAI, OBSERVATION_VERSION
from .candidates import preselect_candidates
from .market import market_snapshot
from .timing import universe_budget_seconds
from .selection import build_observation_selection


def _available_market(project, sector, packet, selection, schedule, directory, master_day, seconds, *, reason, online,
                      benchmark=None, evaluations=None, allow_unknown=True):
    """Keep independently checked market facts after a stock-only preparation gap."""
    gaps = [reason]
    if benchmark is None and seconds > 1:
        try:
            benchmark = prepare_benchmark(project, selection, sector, online=online,
                max_seconds=min(20, seconds), required_for_market=True)
        except (ValueError, OSError, RuntimeError) as exc:
            gaps.append("指数补充未完成：" + type(exc).__name__)
    market = market_snapshot(universe=packet["universe"], quotes=packet["quotes"].get("rows", []),
        benchmark=benchmark or {}, market_date=schedule["market_as_of_date"])
    market.update(status="partial", preparation_limitations=gaps)
    selection_ready = selection.get("selection_verified") is True
    preselection = {"candidates": [], "pending": [], "excluded": [], "baseline_ids": [],
        "selection_id": selection["selection_id"], "market_as_of_date": schedule["market_as_of_date"],
        "observation_only": selection.get("observation_only") is True,
        "rule_version": OBSERVATION_VERSION if selection.get("observation_only") is True else "premarket-recommendation-v1",
        "directions": deepcopy(selection.get("selected_sectors", [])) if selection_ready else []}
    if selection_ready and selection.get("observation_only") is True and (evaluations is not None or allow_unknown):
        # The dated relative-strength evidence survives missing history. These
        # rows explicitly say unknown; they do not substitute prices or scores.
        rows = deepcopy(evaluations) if evaluations is not None else [{"security_id": m["security_id"], "symbol": {"SSE": "sh.", "SZSE": "sz."}[m["exchange"]] + m["code"],
            "name": m.get("name"), "technical_status": "unknown", "eligibility_status": "pending",
            "metrics": {}, "technical_conditions": [], "eligibility_gaps": [reason], "data_issues": [reason]}
            for m in selection["members"]]
        preselection = preselect_candidates(selection, rows,
            {"selection_id": selection["selection_id"], "trade_date": selection["target_date"], "records": []})
    result = {"market": market, "preselection": preselection,
        "company_reviews": {}, "financial_review": None,
        "entities": {row["symbol"]: {key: row[key] for key in ("security_id", "name", "sector_ids")}
            for row in preselection["candidates"]}, "selection_id": selection["selection_id"],
        "universe_observed_date": master_day, "preparation_issues": gaps}
    atomic_json(directory / "market_preparation.json", result)
    return result


def collect_market(project, config, schedule, directory, seconds, run_id, *, universe_max_seconds=900):
    universe_budget_seconds(seconds, universe_max_seconds)
    began = time.monotonic()
    def left(): return max(0.0, seconds - (time.monotonic() - began))
    sector = load_config(project, config.sector_config)
    options = load_options(project, config.observation_config)
    master_day = schedule["report_date"] if schedule["is_trading_day"] else schedule["market_as_of_date"]
    universe_config = load_universe_config(project, config.universe_config)
    if universe_config.calendar_provider != "exchange_calendars":
        raise ValueError("premarket_requires_reviewed_local_calendar")
    if master_day == datetime.now(SHANGHAI).date().isoformat():
        if left() < 1: raise TimeoutError("market_deadline")
        result = sync_date(project=project, config_path=config.universe_config, target=date.fromisoformat(master_day),
                           max_seconds=universe_budget_seconds(left(), universe_max_seconds))
        if not result.get("universe_verified") or not result.get("collection_ready"):
            raise ValueError("premarket_universe_unverified")
    else:
        _read_snapshot(project, {"scope": "sse_szse_a"}, master_day, universe_config)
    if left() < 1: raise TimeoutError("market_deadline")
    source_dir = local_path(project, sector["output_directory"]) / "source_runs" / run_id
    source_dir.mkdir(parents=True, exist_ok=False)
    packet = collect_inputs(project, sector, schedule["market_as_of_date"], source_dir,
                            max_seconds=min(left(), seconds * .4), universe_date=master_day)
    if left() > 1:
        packet = supplement_zero_quotes(project, packet, schedule["market_as_of_date"], source_dir / "dated_status",
            max_seconds=min(options["max_quote_status_seconds"], left()), max_queries=options["max_quote_status_queries"],
            universe_observed_date=master_day if master_day != schedule["market_as_of_date"] else None)
    return prepare_collected_market(project, config, schedule, directory, left(), packet, master_day=master_day)


def prepare_collected_market(project, config, schedule, directory, seconds, packet, *, master_day):
    """Prepare bounded observations from already acquired, validated inputs."""
    began = time.monotonic()
    def left(): return max(0.0, seconds - (time.monotonic() - began))
    sector = load_config(project, config.sector_config)
    options = load_options(project, config.observation_config)
    selection, memberships = evaluate(packet["universe"], packet["catalog"], packet["memberships"], packet["quotes"],
        target=schedule["market_as_of_date"], cutoff=datetime.now(SHANGHAI).isoformat(), config=sector, calendar=packet["calendar"])
    archive_selection(project, sector, selection, memberships, packet)
    selection = build_observation_selection(selection, packet)
    archive_selection(project, sector, selection, memberships, packet)
    if not selection.get("selection_verified"):
        return _available_market(project, sector, packet, selection, schedule, directory, master_day, left(),
            reason="方向或个股研究集合未完整核验，仅使用已有市场事实", online=True)
    benchmark = observation = base = None
    try:
        if left() < 1: raise TimeoutError("market_deadline")
        observation = _observe(project, selection, sector, directory / "observation", left(),
            options["require_delisting_check"], options["strategy_config"], config.eligibility_sources_config)
        # Preserve verified exclusions before optional history-score processing.
        base = build_report(selection, observation, {"status": "skipped"},
            planned_cutoff=datetime.now(SHANGHAI).isoformat(), generated_at=datetime.now(SHANGHAI).isoformat())
        _, inputs, _ = load_reference_inputs(local_path(project, observation["evidence_directory"]))
        benchmark = inputs["benchmark"]
        if not selection["members"] and left() >= 1:
            benchmark = prepare_benchmark(project, selection, sector, online=True, max_seconds=min(20, left()), required_for_market=True)
        elif benchmark.get("verified") is not True and left() >= 1:
            benchmark = prepare_benchmark(project, selection, sector, online=False, max_seconds=min(20, left()), required_for_market=True)
        reference = _reference_review(project, selection, observation, options, directory / "reference")
        if reference is None:
            reference = {"selection_id": selection["selection_id"], "trade_date": selection["target_date"], "records": []}
        preselection = preselect_candidates(selection, base["evaluations"], reference)
    except (ValueError, OSError, RuntimeError) as exc:
        # The failed stage may already have attempted the provider. Recovery can
        # read validated cache, but cannot silently issue a second online call.
        return _available_market(project, sector, packet, selection, schedule, directory, master_day, left(),
            reason="个股历史或评分准备未完成，保留已核验市场事实（" + type(exc).__name__ + "）",
            online=False, benchmark=benchmark, evaluations=base["evaluations"] if base is not None else None,
            allow_unknown=observation is None)
    finance_policy = options.get("financial_review", {})
    financials = None
    if finance_policy.get("enabled") and left() >= 1:
        chosen = [{k: row[k] for k in ("security_id", "symbol", "name")} for row in preselection["candidates"]]
        financials = collect_financial_review(candidates=chosen, target_date=selection["target_date"],
            cutoff_at=datetime.now(SHANGHAI).isoformat(), output_directory=directory / "financial_review",
            cache_directory=project / "data/research/financial_review", max_seconds=min(120, left()),
            max_candidates=5, max_queries=min(20, finance_policy["max_queries"]),
            timeout_seconds=min(finance_policy["timeout_seconds"], left()), online=True)
    entities = {row["symbol"]: {"security_id": row["security_id"], "name": row["name"], "sector_ids": row["sector_ids"]}
                for row in preselection["candidates"]}
    market = market_snapshot(universe=packet["universe"], quotes=packet["quotes"].get("rows", []),
        benchmark=benchmark, market_date=schedule["market_as_of_date"])
    result = {"market": market, "preselection": preselection, "company_reviews": {}, "financial_review": financials,
        "entities": entities, "selection_id": selection["selection_id"], "universe_observed_date": master_day,
        "source_paths": {"selection_id": selection["selection_id"], "observation": observation["evidence_directory"]}}
    atomic_json(directory / "market_preparation.json", result)
    return result
