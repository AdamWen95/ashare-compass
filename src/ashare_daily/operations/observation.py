"""Production sector-first daily observations, sharing the original lock/budget."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time as elapsed
from uuid import uuid4

from .daily import atomic_json, clock_value, local_path, read_runs, resolve_times
from .lock import AlreadyRunning, ProcessLock
from ..market_schemas import SHANGHAI


def load_options(project, path):
    options = json.loads(local_path(project, path).read_text(encoding="utf-8"))
    if (options.get("schema_version") != "daily-observation-config-v1"
            or options.get("company_materials_required_for_technical_observation") is not False
            or options.get("market_data_model_export") is not False
            or type(options.get("model_context_enabled")) is not bool):
        raise ValueError("invalid_observation_policy")
    options.setdefault("require_delisting_check", True)
    if type(options["require_delisting_check"]) is not bool:
        raise ValueError("invalid_observation_delisting_policy")
    for key, upper in (("max_run_seconds", 14400), ("max_quote_status_seconds", 300), ("max_quote_status_queries", 100)):
        if type(options.get(key)) is not int or not 0 < options[key] <= upper:
            raise ValueError("invalid_observation_runtime_limit")
    from ..sector_screening import validate_screening_config
    validate_screening_config(json.loads(local_path(project, options["strategy_config"]).read_text(encoding="utf-8")))
    financial = options.get("financial_review")
    if financial is not None:
        if (not isinstance(financial, dict) or type(financial.get("enabled")) is not bool
                or financial.get("source") != "baostock_public"
                or financial.get("user_authorized") is not True or financial.get("local_storage") is not True
                or financial.get("llm_export") is not False or financial.get("redistribution") is not False):
            raise ValueError("invalid_financial_review_policy")
        for key, upper in (("max_candidates", 5), ("max_queries", 20), ("max_seconds", 120), ("timeout_seconds", 20)):
            if type(financial.get(key)) is not int or not 0 < financial[key] <= upper:
                raise ValueError("invalid_financial_review_limit")
    reference = options.get("reference_strategy")
    if reference is not None and (not isinstance(reference, dict)
            or set(reference) != {"enabled", "application", "changes_candidate_ranking", "llm_export"}
            or type(reference.get("enabled")) is not bool or reference.get("application") != "shadow_only"
            or reference.get("changes_candidate_ranking") is not False or reference.get("llm_export") is not False):
        raise ValueError("invalid_reference_strategy_policy")
    return options


def supplement_zero_quotes(project, packet, target, directory, *, max_seconds, max_queries):
    """Only dated zero/missing-price observations; no inferred suspension."""
    from ..providers.sector_status import _permission, apply_status_evidence, ENDPOINT, SCHEMA
    from ..providers.baostock_f2 import BaoStockF2Client
    from ..sector_pipeline import write_new
    _permission(project)
    members = {({"SSE": "sh", "SZSE": "sz"}.get(m["exchange"], "?") + m["code"]): m
        for m in packet["universe"]["members"] if m.get("security_type") == "ordinary_a"}
    needed = []
    for row in packet["quotes"].get("rows", []):
        if (row.get("trade_date") == target and row.get("date_verified") is True and row.get("close") is None
                and row.get("symbol") in members and row.get("full_day_halt_evidence") is None):
            needed.append(row["symbol"])
    if not needed:
        return packet
    selected = sorted(set(needed))[:max_queries]
    identities = {symbol[:2] + "." + symbol[2:]: {k: members[symbol][k] for k in
        ("security_id", "code", "exchange", "board", "security_type", "metadata_verified")} for symbol in selected}
    write_new(directory / "manifest.json", {"schema_version": SCHEMA, "target_date": target,
        "scope": "sse_szse_a", "mode": "research", "online_requested": True,
        "environment_label": "daily_observation_live", "permission_basis": "existing_registered_baostock_local_dated_state",
        "universe_snapshot_id": packet["universe"]["snapshot_id"], "source_endpoint": ENDPOINT,
        "calendar": packet["calendar"], "identities": identities, "observed_at": datetime.now(SHANGHAI).isoformat(),
        "requested_count": len(identities), "unattempted_count": len(set(needed)) - len(identities),
        "max_seconds": max_seconds, "max_attempts": 1, "long_history_requests": 0, "model_calls": 0})
    online, stopped = {}, None
    deadline = elapsed.monotonic() + max_seconds
    with BaoStockF2Client(timeout_seconds=min(20, max_seconds), max_attempts=1, pause_seconds=0.5,
                         max_requests_per_session=max(1, len(identities))) as client:
        for symbol in identities:
            remaining = deadline - elapsed.monotonic()
            if remaining <= 0:
                stopped = "status_runtime_limit"
                break
            if online:
                elapsed.sleep(min(.5, remaining))
                remaining = deadline - elapsed.monotonic()
                if remaining <= 0:
                    stopped = "status_runtime_limit"
                    break
            client.timeout_seconds = min(20, remaining)
            response = client.query("history_f2", code=symbol, start_date=target, end_date=target,
                security_type="stock", adjustment_mode="unadjusted")
            path = directory / "responses" / (symbol + ".json")
            checksum = write_new(path, response)
            if not response.get("ok"):
                stopped = "status_source_failed"
                break
            online[symbol] = {"path": str(path), "source_file_hash": checksum, "raw_hash": response["raw_hash"]}
    result_path = directory / "result.json"
    write_new(result_path, {"schema_version": SCHEMA, "target_date": target, "cached": {}, "online": online,
        "network_requests": len(list((directory / "responses").glob("*.json"))), "source_stop_reason": stopped,
        "unresolved": sorted(set(identities) - set(online)), "long_history_requests": 0, "model_calls": 0})
    updated = deepcopy(packet)
    # The shared reader independently replays response hashes, SDK completion,
    # date, identity and calendar. Any malformed proof keeps selection blocked.
    updated["quotes"] = apply_status_evidence(project, packet["universe"], packet["quotes"], target, result_path)
    updated["daily_status_result"] = str(result_path)
    return updated


def select_observation(project, config, target, run_id, options, max_seconds):
    from ..sector_pipeline import collect_inputs, archive_selection, write_new, permission_allowed
    from ..sector_selection import evaluate
    if not permission_allowed(config):
        raise ValueError("sector_source_permission_required")
    directory = local_path(project, config["output_directory"]) / "source_runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    began = elapsed.monotonic()
    packet = collect_inputs(project, config, target, directory, max_seconds=max_seconds)
    remaining = max_seconds - (elapsed.monotonic() - began)
    if remaining > 0:
        packet = supplement_zero_quotes(project, packet, target, directory / "dated_status",
            max_seconds=min(options["max_quote_status_seconds"], remaining), max_queries=options["max_quote_status_queries"])
    cutoff = datetime.now(SHANGHAI).isoformat()
    selection, rows = evaluate(packet["universe"], packet["catalog"], packet["memberships"], packet["quotes"],
        target=target, cutoff=cutoff, config=config, calendar=packet["calendar"])
    archive_selection(project, config, selection, rows, packet)
    write_new(directory / "daily_selection_result.json", {"selection_id": selection["selection_id"],
        "selection_status": selection["selection_status"], "actual_cutoff_at": cutoff})
    return selection


def _refresh(project, config, target, now):
    from ..universe_service import sync_date
    return sync_date(project=project, config_path=config.universe_config, target=target,
        cutoff_at=now.isoformat(), now=now)


def _observe(project, selection, sector_config, directory, max_seconds, require_delisting_check, strategy_config, eligibility_config):
    from ..sector_observation import run_observation
    return run_observation(project, selection, sector_config, output_directory=directory,
        online=True, max_seconds=max_seconds, strategy_config_path=strategy_config,
        eligibility_config_path=eligibility_config, require_delisting_check=require_delisting_check)


def _context(project, config, times, run_id, directory, skip_model, *, max_seconds=None):
    from ..research.model_settings import load_model_settings
    from ..research.sources import collect_materials
    from ..research.runner import archive_materials
    from ..sector_observation_model import run_sector_market_context
    if skip_model:
        return {"status": "skipped", "model_run": {"status": "skipped", "call_count": 0}}
    began = elapsed.monotonic()
    try:
        settings = load_model_settings(local_path(project, config.env_file), include_environment=False)
        settings = settings.model_copy(update={"timeout_seconds": float(config.model_timeout_seconds),
            "max_retries": config.model_max_retries, "max_calls": min(2, config.model_max_calls_per_run)})
        if config.model_max_output_tokens:
            settings = settings.model_copy(update={"max_output_tokens": config.model_max_output_tokens})
        if not settings.api_key.get_secret_value():
            result = {"status": "missing_configuration", "model_run": {"status": "missing_configuration", "call_count": 0}}
            atomic_json(directory / "market_context.json", result)
            return result
        collection = collect_materials(registry_path=local_path(project, config.sources_config),
            start=times["query_start_at"], cutoff=times["cutoff_at"], sample_symbols={},
            output_dir=directory / "sources", include_background=True,
            max_seconds=max_seconds * .6 if max_seconds is not None else None)
        bundle = archive_materials(collection=collection, database=local_path(project, config.database),
            output_dir=directory, start=times["query_start_at"], cutoff=times["cutoff_at"])
        result = run_sector_market_context(evidence_bundle=bundle["bundle_file"], start=times["query_start_at"],
            cutoff=times["cutoff_at"], source_registry=local_path(project, config.sources_config),
            budget_database=project / "data/operations/runtime.sqlite3", run_id=run_id, settings=settings,
            max_calls_per_day=min(6, config.model_max_calls_per_day),
            max_seconds=max(.001, max_seconds - (elapsed.monotonic()-began)) if max_seconds is not None else None)
    except Exception as exc:
        # Never archive exception strings from credential-bearing adapters.
        result = {"status": "context_unavailable", "errors": [type(exc).__name__],
            "model_run": {"status": "context_unavailable", "call_count": 0}}
    atomic_json(directory / "market_context.json", result)
    return result


def _financials(project, selection, observation, options, directory, times, *, max_seconds):
    """Enrich only the leading qualified observations; never widen model inputs."""
    policy = options.get("financial_review", {})
    if not policy.get("enabled"):
        return None
    from ..financial_review import collect_financial_review
    passed = {r["security_id"] for r in observation["eligibility"]["evaluations"] if r["eligibility_status"] == "pass"}
    candidates = sorted((r for r in observation["technical"]["evaluations"]
        if r["technical_status"] == "pass" and r["security_id"] in passed),
        key=lambda r: (-Decimal(r["metrics"]["relative_return_20"]), -Decimal(r["metrics"]["avg_amount_20_cny"]), r["symbol"]))
    candidate_limit = min(policy["max_candidates"], observation["technical"]["strategy_config"]["max_candidates"])
    candidates = [{k: r[k] for k in ("security_id", "symbol", "name")} for r in candidates[:candidate_limit]]
    return collect_financial_review(candidates=candidates, target_date=selection["target_date"],
        cutoff_at=times["cutoff_at"], output_directory=directory,
        cache_directory=project / "data/research/financial_review", max_seconds=max(.001, min(policy["max_seconds"], max_seconds)),
        max_candidates=policy["max_candidates"], max_queries=policy["max_queries"],
        timeout_seconds=policy["timeout_seconds"], online=max_seconds > 0)


def _reference_review(project, selection, observation, options, directory):
    if not options.get("reference_strategy", {}).get("enabled"):
        return None
    from ..reference_strategy import build_reference_review, load_reference_inputs
    saved_selection, inputs, saved_observation = load_reference_inputs(local_path(project, observation["evidence_directory"]))
    if saved_selection != selection or saved_observation != observation:
        raise ValueError("reference_daily_observation_differs_from_frozen_files")
    packet = build_reference_review(selection, inputs, observation)
    atomic_json(directory / "reference_review.json", packet)
    return packet


def _publish(project, selection, observation, context, times, *, financial_review=None, reference_review=None):
    from ..reports.observation import publish_observation
    return publish_observation(project, selection, observation, context,
        planned_cutoff=times["cutoff_at"], generated_at=datetime.now(SHANGHAI).isoformat(),
        financial_review=financial_review, reference_review=reference_review)


def run_observation_daily(*, project, config, config_path, target, cutoff, start, now, dry_run,
                          scheduled, planned, max_seconds=None, skip_model=False, force=False):
    from ..sector_pipeline import load_config
    from ..reports.observation import read_observation_report
    project = Path(project).resolve()
    options = load_options(project, config.observation_config)
    sector_config = load_config(project, config.sector_config)
    if sector_config["universe_config"] != config.universe_config or sector_config["database"] != config.database:
        raise ValueError("observation_daily_data_paths_mismatch")
    budget = max_seconds if max_seconds is not None else options["max_run_seconds"]
    if type(budget) not in (int, float) or not 0 < budget <= 14400:
        raise ValueError("observation_runtime_invalid")
    root = local_path(project, config.output_directory)
    previous = read_runs(root, scope="sse_szse_a", research_mode="sector_first")
    times = resolve_times(target, cutoff, start, now, config, previous, planned)
    target = date.fromisoformat(times["target_trade_date"])
    versions = {str(path): hashlib.sha256(local_path(project, path).read_bytes()).hexdigest() for path in
        (config_path, config.observation_config, config.sector_config, config.universe_config, options["strategy_config"],
         config.sources_config, config.eligibility_sources_config,
         *( ["config/node_runtime.json"] if (project / "config/node_runtime.json").is_file() else [])) if path}
    package = Path(__file__).parents[1]
    implementation = {path: hashlib.sha256((package / path).read_bytes()).hexdigest() for path in
        ("operations/observation.py", "sector_observation.py", "sector_observation_model.py", "sector_screening.py",
         "sector_eligibility.py", "reports/observation.py", "reports/m3_render.py", "providers/sector_status.py",
         "financial_review.py", "sector_history.py", "providers/baostock_f2.py", "providers/sina_history.py",
         "reference_strategy.py", "reference_indicators.py")}
    identity = hashlib.sha256(json.dumps({"date": str(target), "cutoff": times["cutoff_at"],
        "query_start": times["query_start_at"], "versions": versions, "implementation": implementation,
        "skip_model": skip_model, "workflow": config.workflow_version}, sort_keys=True).encode()).hexdigest()
    run_id = now.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8]
    directory = root / ("previews" if dry_run else "runs") / run_id
    record = {"schema_version": "m4-run-v1", "run_id": run_id, **times, "started_at": now.isoformat(),
        "scope": "sse_szse_a", "research_mode": "sector_first", "purpose": "production", "production_eligible": True,
        "workflow_version": config.workflow_version, "implementation_stage": "daily_observation",
        "task_identity": identity, "config_versions": versions, "implementation_hashes": implementation,
        "eligibility_policy": {"require_delisting_check": options["require_delisting_check"]},
        "market_runtime_limit_seconds": budget, "trigger_kind": "scheduled" if scheduled else "manual",
        "generation_status": "not_run", "status": "running", "module_statuses": {},
        "model_summary": {"status": "not_run", "call_count": 0}, "run_directory": str(directory),
        "external_calls_allowed": not dry_run}
    began = elapsed.monotonic()
    def finish(status, code, **details):
        record.update(status=status, exit_code=code, **details)
        record.update(ended_at=datetime.now(SHANGHAI).isoformat(), duration_seconds=round(elapsed.monotonic()-began, 3))
        atomic_json(directory / "result.json", record)
        return record
    if dry_run:
        return finish("dry_run", 0, steps=["可信交易日历与动态名单", "行业筛选及有据停牌补证", "仅关注集合历史与量价资格核验",
            "本地观察名单", "参考策略技术辅助评分（试运行）", "优先候选免费财务核查",
            "许可允许的新闻模型背景", "冻结报告发布"], scheduled_entry_enabled=True)
    try:
        with ProcessLock(project / "data/operations/daily.lock", run_id):
            if scheduled and now < clock_value(times["planned_trigger_at"]):
                return finish("not_due", 0)
            if target == now.date() and now < datetime.combine(target, time(21), SHANGHAI):
                return finish("not_due", 0)
            current_runs = read_runs(root, scope="sse_szse_a", research_mode="sector_first")
            for old in current_runs:
                if old.get("status") == "running":
                    old.update(status="interrupted", exit_code=2, ended_at=datetime.now(SHANGHAI).isoformat())
                    atomic_json(root / "runs" / old["run_id"] / "result.json", old)
                if not force and old.get("task_identity") == identity and old.get("generation_status") == "ok":
                    try:
                        prior = read_observation_report(project / "outputs", Path(old["report"]["directory"]))
                        if prior["report"]["trade_date"] != str(target):
                            continue
                    except (OSError, KeyError, ValueError, TypeError):
                        continue
                    return finish("reused", 0, report=old["report"], generation_status="ok", original_run_id=old["run_id"])
            attempts = [r for r in current_runs if r.get("trigger_kind") == "scheduled" and r.get("started_at", "")[:10] == now.date().isoformat()
                and r.get("status") not in {"reused", "already_running", "dry_run", "not_due"}]
            if scheduled and len(attempts) >= config.scheduled_attempts_per_day:
                return finish("catchup_limit", 2)
            atomic_json(directory / "result.json", record)
            universe = _refresh(project, config, target, now)
            record.update(universe_result=universe, configured_stock_count=universe.get("ordinary_a_count"))
            record["module_statuses"]["universe"] = universe["status"]
            if universe["status"] == "non_trading_day":
                return finish("non_trading_day", 0)
            if not universe.get("universe_verified") or not universe.get("collection_ready"):
                return finish("universe_blocked", 2)
            remaining = budget - (elapsed.monotonic()-began)
            if remaining <= 0:
                return finish("market_runtime_limit", 2)
            selection = select_observation(project, sector_config, str(target), run_id, options, remaining * 0.4)
            record.update(selection_id=selection["selection_id"])
            record["module_statuses"]["selection"] = selection["selection_status"]
            if not selection.get("selection_verified"):
                return finish("selection_blocked", 2)
            remaining = budget - (elapsed.monotonic()-began)
            if remaining <= 0:
                return finish("market_runtime_limit", 2)
            financial_policy = options.get("financial_review", {})
            reserve = min(financial_policy.get("max_seconds", 0), remaining * .15) if financial_policy.get("enabled") else 0
            observation = _observe(project, selection, sector_config, directory / "observation", remaining - reserve,
                options["require_delisting_check"], options["strategy_config"], config.eligibility_sources_config)
            record["module_statuses"]["screening"] = observation["status"]
            record["observation_counts"] = observation["counts"]
            reference_review = None
            if options.get("reference_strategy", {}).get("enabled"):
                if budget - (elapsed.monotonic()-began) <= 0:
                    record["module_statuses"]["reference_strategy"] = "skipped_runtime_limit"
                else:
                    reference_review = _reference_review(project, selection, observation, options, directory / "reference_strategy")
                    if reference_review is not None:
                        record["module_statuses"]["reference_strategy"] = "shadow_only"
                        record["reference_summary"] = {"content_hash": reference_review["content_hash"],
                            "scored_count": sum(row["status"] == "available" for row in reference_review["records"]),
                            "changes_candidate_ranking": False, "network_requests": 0, "model_calls": 0}
            remaining = budget - (elapsed.monotonic()-began)
            financial_review = _financials(project, selection, observation, options, directory / "financial_review", times,
                max_seconds=max(0, remaining))
            if financial_review is not None:
                record["module_statuses"]["financial_review"] = financial_review["status"]
                record["financial_summary"] = {key: financial_review.get(key) for key in
                    ("status", "network_requests", "cache_hits", "content_hash")}
                record["financial_summary"]["reviewed_count"] = len(financial_review.get("records", []))
            remaining = budget - (elapsed.monotonic()-began)
            if remaining <= 0:
                context = {"status": "skipped_runtime_limit", "model_run": {"status": "skipped_runtime_limit", "call_count": 0}}
            else:
                context = _context(project, config, times, run_id, directory / "context",
                    skip_model or not options["model_context_enabled"], max_seconds=remaining)
            record["model_summary"] = context.get("model_run", {"status": context["status"], "call_count": 0})
            record["module_statuses"]["model"] = context["status"]
            published = _publish(project, selection, observation, context, times,
                financial_review=financial_review, reference_review=reference_review)
            record["module_statuses"]["publish"] = "ok"
            partial = observation["counts"].get("blocking_gap_count", 0) > 0 or context["status"] not in {"ok", "skipped", "no_eligible_evidence"}
            return finish("partial" if partial else "ok", 1 if partial else 0,
                generation_status="ok", report=published, notice="公司材料缺失不阻塞量价观察；资格未知继续待查。")
    except AlreadyRunning:
        return finish("already_running", 3)
    except Exception as exc:
        return finish("failed", 2, failure_reason="量价日任务失败：" + type(exc).__name__,
            last_completed_modules=record["module_statuses"])
