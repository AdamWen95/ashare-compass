"""Single morning job: bounded phases, preserved attempts, actual publication."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .daily import PROJECT, atomic_json, local_path, read_runs
from .lock import ProcessLock, AlreadyRunning
from ..premarket.archive import publish_brief, scan_briefs
from ..premarket.candidates import finalize_candidates
from ..premarket.company import merge_company_reviews
from ..premarket.contracts import SHANGHAI, OBSERVATION_VERSION, aware
from ..premarket.decisions import decide_stance
from ..premarket.report import build_brief
from ..premarket.timing import resolve_schedule, request_window, stage_seconds
from ..sector_selection import digest


class WallClock:
    def now(self): return datetime.now(SHANGHAI)
    def sleep(self, seconds): time.sleep(seconds)


def load_policy(project, config):
    policy = json.loads(local_path(project, config.premarket_config).read_text(encoding="utf-8"))
    if (policy.get("schema_version") != "premarket-policy-v1" or type(policy.get("production_enabled")) is not bool
            or policy.get("news_max_requests") != 20 or type(policy.get("market_max_seconds")) is not int
            or not 0 < policy["market_max_seconds"] <= 1200
            or type(policy.get("universe_max_seconds", 900)) is not int
            or not 1 <= policy.get("universe_max_seconds", 900) <= min(900, policy["market_max_seconds"])
            or policy.get("required_roles") != ["macro_policy", "securities_regulation", "company_announcements"]):
        raise ValueError("premarket_policy_invalid")
    allowed = {"pbc_policy_news": ["macro_policy"], "csrc_regulation_news": ["securities_regulation"],
               "cninfo_company_announcements": ["company_announcements"],
               "mof_fiscal_news": ["macro_policy"], "szse_original_news": ["securities_regulation"]}
    seen = set()
    for source in policy.get("local_sources", []):
        reg = source.get("registration", {})
        key = reg.get("source_id")
        if (key not in allowed or key in seen or source.get("research_roles") != allowed[key]
                or type(reg.get("enabled")) is not bool or reg.get("model_use_allowed") is not False
                or reg.get("publish_excerpt_allowed") is not False
                or (reg["enabled"] and (reg.get("cache_allowed") is not True or not reg.get("permission_basis")))):
            raise ValueError("premarket_local_source_policy_invalid")
        aware(reg.get("checked_at", ""))
        seen.add(key)
    return policy


def _reserved_for(project, run_ids):
    database = project / "data/operations/runtime.sqlite3"
    if not database.is_file() or not run_ids:
        return False
    # Read only; an uncertain previous request is never refunded or reissued.
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='m4_model_reservations'").fetchone():
            return False
        return any(db.execute("SELECT 1 FROM m4_model_reservations WHERE run_id=? LIMIT 1", (key,)).fetchone() for key in run_ids)


def run_premarket_daily(*, project, config, config_path, target, cutoff, start, now, dry_run,
                        scheduled, planned, max_seconds=None, skip_model=False, force=False,
                        services=None, clock=None) -> dict:
    project, now = Path(project).resolve(), aware(now)
    if (services is not None or clock is not None) and project == PROJECT.resolve():
        raise ValueError("test_services_rejected_for_production")
    clock = clock or WallClock()
    policy = load_policy(project, config)
    if services is None:
        from ..premarket.services import PremarketServices
        services = PremarketServices(project, config, policy)
    run_id = now.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8]
    root = local_path(project, config.output_directory)
    directory = root / ("previews" if dry_run else "runs") / run_id
    record = {"schema_version": "m4-run-v1", "run_id": run_id, "scope": "sse_szse_a",
        "research_mode": "sector_first", "workflow_version": "premarket-daily-v1",
        "trigger_kind": "scheduled" if scheduled else "manual", "started_at": now.isoformat(),
        "status": "running", "generation_status": "not_run", "module_statuses": {}}
    def finish(status, code=0, **details):
        record.update(status=status, exit_code=code, ended_at=clock.now().isoformat(), **details)
        atomic_json(directory / "result.json", record)
        return record
    try:
        schedule = resolve_schedule(now, services.calendar(now), report_date=target)
    except (ValueError, OSError):
        return finish("calendar_unverified", 2, failure_reason="premarket_calendar_unverified")
    if cutoff is not None and aware(cutoff) != aware(schedule["news_cutoff_at"]):
        raise ValueError("premarket_cutoff_must_match_edition")
    record.update(schedule, target_trade_date=schedule["target_session_date"], cutoff_at=schedule["news_cutoff_at"])
    config_hashes = {path: hashlib.sha256(local_path(project, path).read_bytes()).hexdigest() for path in
        (config_path, config.premarket_config, config.observation_config, config.sector_config, config.universe_config, config.sources_config)}
    source_paths = [Path(__file__), *sorted((Path(__file__).parents[1] / "premarket").glob("*.py"))]
    implementation = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    identity = digest({"date": schedule["report_date"], "configs": config_hashes, "implementation": implementation})
    record["task_identity"] = identity
    if dry_run:
        return finish("dry_run", steps=["上一完整交易日行情", "评分预选及公司资料", "盘前消息", "市场姿态", "09:00发布"],
                      production_enabled=policy["production_enabled"], external_calls=0)
    if scheduled and schedule["timing_status"] == "not_due":
        return finish("not_due")
    if scheduled and schedule["timing_status"] == "missed":
        return finish("missed_window", 2)
    if not policy["production_enabled"]:
        return finish("release_gate_blocked", 2, failure_reason=policy.get("release_status", "pending_acceptance"))
    try:
        with ProcessLock(project / "data/operations/daily.lock", run_id):
            previous = read_runs(root, scope="sse_szse_a", research_mode="sector_first")
            archives, _ = scan_briefs(project / "outputs", now=clock.now())
            if not force:
                recovered = next((a for a in archives if a["report"].get("task_identity") == identity), None)
                if recovered:
                    return finish("reused", generation_status="ok", original_run_id=recovered["report"].get("run_id"),
                        report={"directory": str(recovered["directory"]), "report_id": recovered["report"]["report_id"], "reused": True})
            for old in previous:
                if not force and old.get("task_identity") == identity and old.get("generation_status") == "ok":
                    matches = [a for a in archives if a["report"]["report_id"] == old.get("report", {}).get("report_id")]
                    if matches:
                        return finish("reused", generation_status="ok", report=old["report"], original_run_id=old["run_id"])
            attempts = [r for r in previous if r.get("trigger_kind") == "scheduled"
                and r.get("started_at", "")[:10] == now.date().isoformat()
                and r.get("status") not in {"reused", "already_running", "dry_run", "not_due", "release_gate_blocked", "missed_window"}]
            if scheduled and len(attempts) >= min(2, config.scheduled_attempts_per_day):
                return finish("catchup_limit", 2)
            uncertain = [r["run_id"] for r in previous if r.get("task_identity") == identity
                         and r.get("status") in {"running", "failed", "interrupted"}]
            skip_model = skip_model or _reserved_for(project, uncertain)
            record["model_suppressed_after_uncertain_attempt"] = bool(skip_model and uncertain)
            atomic_json(directory / "result.json", record)
            prior = next((a["report"] for a in archives if a["report"]["report_date"] < schedule["report_date"]), None)
            watermarks = [a["report"]["news_successful_cutoff_at"] for a in archives
                if a["report"].get("news_successful_cutoff_at") and aware(a["report"]["news_successful_cutoff_at"]) < aware(schedule["news_cutoff_at"])]
            watermark = max(watermarks, key=aware) if watermarks else None
            attempted_starts = [r["query_start_at"] for r in previous if r.get("workflow_version") == "premarket-daily-v1"
                and r.get("query_start_at") and r.get("status") != "dry_run"]
            window = request_window(schedule, start or watermark or (min(attempted_starts, key=aware) if attempted_starts else None))
            record["query_start_at"] = window["start_at"]
            def remaining(stage):
                value = stage_seconds(schedule, stage, clock.now())
                if max_seconds is not None:
                    value = min(value, max(0, max_seconds-(clock.now()-now).total_seconds()))
                return value
            empty_preselection = {"candidates": [], "pending": [], "excluded": [], "directions": [], "baseline_ids": [],
                "observation_only": True, "rule_version": OBSERVATION_VERSION,
                "market_as_of_date": schedule["market_as_of_date"]}
            prepared = {"market": {"status": "partial", "market_as_of_date": schedule["market_as_of_date"], "issues": ["market_unavailable"]},
                        "preselection": empty_preselection, "company_reviews": {}, "entities": {}}
            for stage in ("market", "news", "decision"):
                record["current_stage"] = stage
                atomic_json(directory / "result.json", record)
                try:
                    if stage == "market":
                        if remaining(stage) > 0:
                            prepared = services.market(schedule, directory, min(policy["market_max_seconds"], remaining(stage)), run_id)
                        record["module_statuses"][stage] = prepared["market"]["status"]
                    elif stage == "news":
                        news = services.news(schedule, window, prepared, directory, remaining(stage)) if remaining(stage) > 0 else {}
                        record["module_statuses"][stage] = "ready" if news.get("coverage", {}).get("ready") else "partial"
                    else:
                        while clock.now() < aware(schedule["news_cutoff_at"]):
                            clock.sleep(min(1, (aware(schedule["news_cutoff_at"])-clock.now()).total_seconds()))
                        interpreted = services.interpret(news, prepared, schedule, directory, run_id, remaining(stage), skip_model) if remaining(stage) > 0 else {}
                        record["model_summary"] = interpreted.get("model_run", {"call_count": 0, "status": "skipped_deadline"})
                        record["module_statuses"][stage] = record["model_summary"].get("status")
                except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
                    record["module_statuses"][stage] = "unavailable"
                    record.setdefault("errors", []).append({"stage": stage, "type": type(exc).__name__})
                    if stage == "news": news = {}
                    if stage == "decision": interpreted = {}
            coverage = news.get("coverage", {"ready": False, "roles": {}})
            events = news.get("local_events", []) + interpreted.get("events", [])
            adverse = any(e.get("validation_status") == "verified" and e.get("risk_status") == "adverse"
                          and e.get("impact_scope") == "market" for e in events)
            stance = decide_stance(prepared["market"], news_ready=coverage.get("ready") is True,
                has_direction=bool(prepared["preselection"]["directions"]), adverse_event=adverse,
                independent=prepared["preselection"].get("observation_only") is True)
            company = merge_company_reviews(prepared["preselection"], news.get("company_reviews", {}), prepared.get("financial_review"))
            candidates = finalize_candidates(prepared["preselection"], company_reviews=company, stance=stance, events=events)
            report = build_brief(schedule=schedule, market=prepared["market"], stance=stance, candidates=candidates,
                events=events, coverage=coverage, previous=prior, generated_at=clock.now().isoformat())
            covered_until = coverage.get("covered_until_at")
            if coverage.get("ready") is True and covered_until and aware(window["start_at"]) < aware(covered_until) <= aware(schedule["news_cutoff_at"]):
                watermark = covered_until
            report.update(task_identity=identity, run_id=run_id, news_query_start_at=window["start_at"],
                news_successful_cutoff_at=watermark, news_window_truncated=window["coverage_gap"])
            atomic_json(directory / "decision_inputs.json", {"prepared": prepared, "events": events, "coverage": coverage})
            if clock.now() < aware(schedule["publication_due_at"]):
                publish_brief(project, report, now=clock.now())
            while clock.now() < aware(schedule["publication_due_at"]):
                clock.sleep(min(1, (aware(schedule["publication_due_at"])-clock.now()).total_seconds()))
            published = publish_brief(project, report, now=clock.now())
            return finish("ok" if report["research_status"] == "ready" else "partial",
                0 if report["research_status"] == "ready" else 1, generation_status="ok", report=published)
    except AlreadyRunning:
        return finish("already_running", 3)
    except (ValueError, OSError, RuntimeError) as exc:
        return finish("failed", 2, failure_reason=type(exc).__name__)
