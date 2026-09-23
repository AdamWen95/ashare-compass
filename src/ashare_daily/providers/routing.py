"""Bounded, observable whole-response fallback; never merge provider windows."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from .base import DailyBarRequest, MarketDataProvider, ProviderResult


ROUTING_VERSION = "f2-provider-routing-v1"
ACCESS_STOPS = {"permission_required", "permission_denied", "rate_limited"}


@dataclass
class RoutedResult:
    selected: ProviderResult
    candidates: list[ProviderResult]
    events: list[dict[str, Any]] = field(default_factory=list)

    def metrics(self) -> dict[str, Any]:
        return {"requests": sum(r.metrics.get("requests", 0) for r in self.candidates),
                "retries": sum(r.metrics.get("retries", 0) for r in self.candidates),
                "fallback_attempts": sum(r.fallback_level > 0 and r.metrics.get("requests", 0) > 0 for r in self.candidates),
                "fallback_selected": int(self.selected.fallback_level > 0 and self.selected.has_facts),
                "fallback_verified": int(self.selected.fallback_level > 0 and self.selected.usable),
                "providers": [{"provider": r.provider, "status": r.status, "selected": r is self.selected,
                               "fallback_level": r.fallback_level, **r.metrics} for r in self.candidates]}


class ProviderRouter:
    def __init__(self, providers: Sequence[MarketDataProvider], *, failure_threshold: int = 3):
        if not providers or len({p.name for p in providers}) != len(providers):
            raise ValueError("a unique, ordered provider chain is required")
        if type(failure_threshold) is not int or not 1 <= failure_threshold <= 3:
            raise ValueError("provider failure threshold cannot exceed the existing three failures")
        self.providers = tuple(providers)
        self.failure_threshold = failure_threshold
        self.failures = Counter()
        self.stopped: dict[str, dict[str, Any]] = {}

    def fetch_daily_bars(self, request: DailyBarRequest) -> RoutedResult:
        candidates, events = [], []
        for level, provider in enumerate(self.providers):
            if provider.name in self.stopped:
                result = ProviderResult(provider.name, "daily_bars", "circuit_open",
                    {"provider": provider.name, "operation": "daily_bars", "ok": False,
                     "status": "circuit_open", "error_code": "circuit_open",
                     "error_msg": "provider stopped for this invocation", "network_sent": False,
                     "stop_evidence": self.stopped[provider.name], "attempts": []},
                    source_symbol=request.identity.source_symbol(provider.name), metrics={"requests": 0, "retries": 0})
            else:
                # Provider adapters classify expected transport/schema failures.
                # Programming errors propagate; an exception is never an empty market.
                result = provider.fetch_daily_bars(request)
                if result.provider != provider.name:
                    raise ValueError("provider result identity differs from routed provider")
            result.fallback_level = level
            candidates.append(result)
            if result.usable:
                self.failures[provider.name] = 0
                if level:
                    events.append({"event": "fallback_selected", "operation": "daily_bars", "provider": provider.name,
                                   "fallback_level": level, "security_id": request.identity.security_id,
                                   "adjustment_mode": request.adjustment_mode, "whole_response": True})
                return RoutedResult(result, candidates, events)
            if result.status in ACCESS_STOPS:
                self.stopped[provider.name] = {"status": result.status, "error_code": result.response.get("error_code")}
            elif result.status != "circuit_open" and result.response.get("ok") is not True:
                self.failures[provider.name] += 1
                if self.failures[provider.name] >= self.failure_threshold:
                    self.stopped[provider.name] = {"status": "consecutive_source_failures",
                                                    "error_code": result.response.get("error_code"),
                                                    "count": self.failures[provider.name]}
            else:
                self.failures[provider.name] = 0
            events.append({"event": "provider_not_usable", "provider": provider.name, "operation": "daily_bars",
                           "security_id": request.identity.security_id, "symbol": request.identity.symbol,
                           "start_date": request.start_date, "end_date": request.end_date,
                           "adjustment_mode": request.adjustment_mode, "fallback_level": level,
                           "source_status": result.status, "error_code": result.response.get("error_code"),
                           "error_type": "quality_invalid_or_incomplete" if result.response.get("ok") else result.status,
                           "failure_stage": result.response.get("diagnostics", {}).get("failure_stage"),
                           "attempts": result.metrics.get("requests", 0),
                           "fallback_to": self.providers[level + 1].name if level + 1 < len(self.providers) else None,
                           "quality_missing_dates": len(result.quality.get("missing_dates", [])),
                           "quality_issue_count": len(result.quality.get("quality_issues", []))})
        # Partial facts may be archived/saved, but no completeness gate is promoted.
        # Prefer the first source with facts; do not splice better rows from others.
        selected = next((r for r in candidates if r.has_facts), candidates[0])
        events.append({"event": "provider_chain_incomplete", "selected_provider": selected.provider,
                       "partial_facts_retained": selected.has_facts, "quote_complete": False,
                       "research_ready": False, "security_id": request.identity.security_id})
        return RoutedResult(selected, candidates, events)

    def close(self) -> None:
        errors = []
        for provider in self.providers:
            try:
                provider.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
