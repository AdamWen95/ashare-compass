"""Live adapters. Construction and calendar resolution make no paid requests."""
from __future__ import annotations

from datetime import date, datetime
import json

from ..operations.daily import atomic_json, local_path
from ..providers.exchange_calendar import ensure_packet, verify_packet
from ..research.sources import collect_materials, load_source_registry
from ..research.runner import archive_materials
from ..universe_service import load_universe_config
from .contracts import SHANGHAI, aware
from .source_policy import coverage_summary
from .source_http import SourceSession
from .policy_sources import collect_policy_sources
from .announcements import collect_company_reviews, SOURCE as COMPANY_SOURCE


class PremarketServices:
    def __init__(self, project, config, policy):
        self.project, self.config, self.policy = project, config, policy

    def calendar(self, now):
        config = load_universe_config(self.project, self.config.universe_config)
        if config.calendar_provider != "exchange_calendars":
            raise ValueError("premarket_calendar_provider_not_reviewed")
        path = ensure_packet(now.date(), local_path(self.project, config.calendar_cache), mode="research")
        _, values = verify_packet(json.loads(path.read_text(encoding="utf-8")), mode="research")
        return {day.isoformat(): opened for day, opened in values.items()}

    def market(self, schedule, directory, seconds, run_id):
        from .services_market import collect_market
        return collect_market(self.project, self.config, schedule, directory, seconds, run_id,
            universe_max_seconds=self.policy.get("universe_max_seconds", 900))

    def news(self, schedule, window, prepared, directory, seconds):
        path = local_path(self.project, self.config.sources_config)
        registry = load_source_registry(path)
        for source in registry["sources"]:
            source["research_roles"] = self.policy.get("source_roles", {}).get(source["registration"]["source_id"], [])
        if registry["request_limits"]["max_requests"] > self.policy["news_max_requests"]:
            raise ValueError("premarket_news_budget_exceeded")
        until = (aware(schedule["news_cutoff_at"])-datetime.now(SHANGHAI)).total_seconds()
        if until <= 0 or seconds <= 0:
            return {"coverage": coverage_summary(registry, {}, required_roles=self.policy["required_roles"]), "company_reviews": {}}
        session = SourceSession(directory / "local_sources", max_requests=self.policy["news_max_requests"],
            max_seconds=max(.001, min(seconds, until)-.1),
            observation_database=self.project / "data/operations/premarket_sources.sqlite3")
        local_sources = self.policy.get("local_sources", [])
        registry["sources"].extend(local_sources)
        enabled = {s["registration"]["source_id"] for s in local_sources if s["registration"].get("enabled") is True}
        local = collect_policy_sources(session, start=window["start_at"], cutoff=schedule["news_cutoff_at"],
            directory=directory / "local_sources", enabled=enabled)
        company = collect_company_reviews(session, prepared["preselection"]["candidates"],
            start=window["start_at"], cutoff=schedule["news_cutoff_at"], directory=directory / "local_sources") if COMPANY_SOURCE in enabled else {"reviews": {}, "status": "disabled"}
        reviews = company["reviews"]
        company_times = [r["covered_until_at"] for r in reviews.values() if r.get("covered_until_at")]
        local["source_health"].append({"source_id": COMPANY_SOURCE,
            "status": "ok" if company["status"] in {"ready", "not_applicable"} else company["status"],
            "failures": [gap for r in reviews.values() for gap in r.get("gaps", [])],
            "covered_until_at": min(company_times, key=aware) if company_times else None,
            "semantic_review_required": any(r.get("risk_status") == "review_required" for r in reviews.values()),
            "notice": "公司核查仅覆盖本期最多5只预选股；没有预选时不发起公司请求"})
        actual_cutoff = schedule["news_cutoff_at"] if until <= 300 else datetime.now(SHANGHAI).isoformat()
        # Licensed text for the existing model is an optional supplement after
        # the required local sources. It shares every HTTP reservation.
        collection, bundle_file = {}, None
        remaining = min(session.remaining(), (aware(schedule["news_cutoff_at"])-datetime.now(SHANGHAI)).total_seconds())
        if session.request_count < session.budget.maximum and remaining > 1:
            try:
                collection = collect_materials(registry_path=path, start=window["start_at"], cutoff=actual_cutoff,
                    sample_symbols={symbol: value["name"] for symbol, value in prepared.get("entities", {}).items()},
                    output_dir=directory / "sources", include_background=True,
                    max_seconds=max(.001, remaining-.1), pending_cutoff=until <= 300, request_budget=session.budget)
                bundle = archive_materials(collection=collection, database=local_path(self.project, self.config.database),
                    output_dir=directory / "context", start=window["start_at"], cutoff=schedule["news_cutoff_at"])
                bundle_file = bundle["bundle_file"]
            except (ValueError, OSError, RuntimeError) as exc:
                local["supplement_error"] = type(exc).__name__
        combined = {**collection, "source_health": collection.get("source_health", []) + local["source_health"],
            "requests": collection.get("requests", []) + session.requests, "request_count": session.request_count,
            "news_cutoff": schedule["news_cutoff_at"], "finished_at": datetime.now(SHANGHAI).isoformat()}
        coverage = coverage_summary(registry, combined, required_roles=self.policy["required_roles"])
        coverage["registered_scope"] = {s["registration"]["source_id"]: s.get("coverage_scope", s.get("access_method", ""))
            for s in local_sources if s["registration"].get("enabled") is True}
        coverage["uncovered_roles"] = ["央行及国务院直接政策栏目", "证监会及上交所监管通知", "海外市场、汇率及期货的综合资讯"]
        result = {"coverage": coverage, "bundle_file": bundle_file, "company_reviews": reviews,
                  "local_events": local["events"], "window": window, "collection_file": collection.get("bundle_file"),
                  "local_sources_directory": str(session.directory), "supplement_error": local.get("supplement_error")}
        atomic_json(directory / "news_preparation.json", result)
        return result

    def interpret(self, news, prepared, schedule, directory, run_id, seconds, skip_model):
        from .model import run_event_context
        return run_event_context(self.project, self.config, news, prepared, schedule, directory, run_id, seconds, skip_model)
