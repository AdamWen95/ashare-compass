"""Provider contracts for F2 facts, independent of discovery and research rules.

Raw responses remain audit evidence; callers consume normalized records and
explicit quality results. The existing M1 BaoStock contracts are not changed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence, TypedDict


BOARD_EXCHANGES = {"sse_main": "SSE", "star": "SSE", "szse_main": "SZSE", "chinext": "SZSE"}
ADJUSTMENTS = {"unadjusted", "forward_adjusted", "backward_adjusted"}


def iso_date(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("explicit YYYY-MM-DD date required")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("explicit YYYY-MM-DD date required")
    return result


@dataclass(frozen=True)
class SecurityIdentity:
    security_id: str
    code: str
    exchange: str
    board: str
    scope: str = "sse_szse_a"
    metadata_verified: bool = False
    security_type: str = "ordinary_a"

    def __post_init__(self):
        if self.scope not in {"sse_szse_a", "all_a"}:
            raise ValueError("unsupported universe scope")
        if self.metadata_verified is not True or not self.security_id or self.security_type != "ordinary_a":
            raise ValueError("source-verified ordinary A-share identity required")
        if self.exchange not in {"SSE", "SZSE"} or BOARD_EXCHANGES.get(self.board) != self.exchange:
            raise ValueError("provider supports the four SSE/SZSE boards only; BSE is not a fallback")
        if not isinstance(self.code, str) or len(self.code) != 6 or not self.code.isascii() or not self.code.isdigit():
            raise ValueError("source-verified six-digit code required")

    @property
    def symbol(self) -> str:
        return {"SSE": "sh.", "SZSE": "sz."}[self.exchange] + self.code

    def source_symbol(self, provider: str) -> str:
        # Exchange is verified metadata; never infer exchange/board from a prefix.
        if provider == "baostock":
            return self.symbol
        if provider == "eastmoney":
            return {"SSE": "1.", "SZSE": "0."}[self.exchange] + self.code
        raise ValueError("unsupported provider mapping")


@dataclass(frozen=True)
class DailyBarRequest:
    identity: SecurityIdentity
    start_date: str
    end_date: str
    expected_dates: tuple[str, ...]
    adjustment_mode: str = "unadjusted"

    def __post_init__(self):
        start, end = iso_date(self.start_date), iso_date(self.end_date)
        if start > end or (end - start).days > 730 or self.adjustment_mode not in ADJUSTMENTS:
            raise ValueError("unsupported date window or explicit adjustment mode")
        dates = tuple(self.expected_dates)
        if not dates or len(dates) > 500 or dates != tuple(sorted(set(dates))):
            raise ValueError("verified, unique, ordered trading dates required (at most 500)")
        if any(not start <= iso_date(day) <= end for day in dates):
            raise ValueError("expected dates outside request window")
        object.__setattr__(self, "expected_dates", dates)

    def parameters(self) -> dict[str, str]:
        return {"code": self.identity.symbol, "start_date": self.start_date, "end_date": self.end_date,
                "security_type": "stock", "adjustment_mode": self.adjustment_mode}


class DailyBar(TypedDict):
    """Normalized numerical facts; provenance lives beside immutable fact hashes."""
    security_id: str
    provider: str
    symbol: str
    trade_date: str
    adjustment_mode: str
    open: str | None
    high: str | None
    low: str | None
    close: str | None
    preclose: str | None
    volume_shares: int | None
    amount_cny: str | None
    tradestatus: bool | None
    is_st: bool | None
    quality_flags: list[str]


@dataclass
class ProviderResult:
    provider: str
    operation: str
    status: str
    response: dict[str, Any]
    records: list[DailyBar] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    source_symbol: str | None = None
    source_endpoint: str | None = None
    source_business_date: str | None = None
    fetched_at: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    fallback_level: int = 0

    @property
    def usable(self) -> bool:
        return self.response.get("ok") is True and self.quality.get("quote_complete") is True

    @property
    def has_facts(self) -> bool:
        return self.response.get("ok") is True and bool(self.records)

    def provenance(self) -> dict[str, Any]:
        kind = self.metrics.get("verification_kind", self.response.get("verification_kind", "unverified"))
        verified = "verified" if kind == "live_network" else "sample_verified" if kind == "offline_test" else "unverified"
        return {"provider": self.provider, "source_endpoint": self.source_endpoint,
                "source_symbol": self.source_symbol, "source_business_date": self.source_business_date,
                "fetched_at": self.fetched_at, "fallback_level": self.fallback_level,
                "verification_status": verified if self.usable else self.status, "verification_kind": kind,
                "research_ready": False}


@dataclass
class QuoteSnapshot:
    security_id: str
    provider: str
    symbol: str
    source_symbol: str
    source_timestamp: str | None
    fetched_at: str
    price: str | None
    prev_close: str | None
    open: str | None
    high: str | None
    low: str | None
    volume_shares: int | None
    amount_cny: str | None
    name: str | None = None
    quality_flags: list[str] = field(default_factory=list)
    # A quote is an observation; it is never a verified closing daily bar.
    is_closing_daily_bar: bool = False


@dataclass
class QuoteResult:
    provider: str
    status: str
    snapshot: QuoteSnapshot | None
    response: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)


class MarketDataProvider(ABC):
    name: str
    capabilities: frozenset[str] = frozenset()

    @abstractmethod
    def fetch_daily_bars(self, request: DailyBarRequest) -> ProviderResult:
        """Return one source and one adjustment version, including failed evidence."""

    def fetch_quote(self, identity: SecurityIdentity, *, target_date: str) -> QuoteResult:
        return QuoteResult(self.name, "unsupported", None,
                           {"ok": False, "error_code": "unsupported_operation", "operation": "quote", "provider": self.name},
                           {"requests": 0, "retries": 0})

    def fetch_universe(self, *args, **kwargs):
        raise NotImplementedError("discovery remains with the verified exchange universe service")

    def fetch_trading_calendar(self, *args, **kwargs):
        raise NotImplementedError("this provider has no verified calendar capability")

    def healthcheck(self, requests: Sequence[DailyBarRequest]) -> dict[str, Any]:
        if len(requests) != 4 or {r.identity.board for r in requests} != set(BOARD_EXCHANGES):
            raise ValueError("healthcheck requires exactly one source-verified identity per board")
        results = []
        for request in requests:
            result = self.fetch_daily_bars(request)
            results.append({"board": request.identity.board, "symbol": request.identity.symbol,
                            "status": result.status, "usable": result.usable, "has_facts": result.has_facts,
                            "response_ok": result.response.get("ok") is True,
                            "provenance": result.provenance(), "metrics": result.metrics,
                            "error_code": result.response.get("error_code")})
            if result.status in {"permission_required", "permission_denied", "rate_limited", "circuit_open"}:
                break
        successes = sum(r["usable"] for r in results)
        available = any(r["has_facts"] for r in results)
        return {"provider": self.name, "status": "healthy" if successes == 4 else "degraded" if available else "unavailable",
                "sample_only": True, "full_market_verified": False, "records": results, "model_calls": 0}

    def close(self) -> None:
        pass
