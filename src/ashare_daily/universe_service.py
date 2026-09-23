"""F1 live discovery boundary. Unknown metadata is evidence of a gap, never A-share proof.

Website adapters require explicit, per-site purpose and access registration.
No market prices, model requests or sample fallback are part of this module.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.operations.daily import atomic_json, local_path
from ashare_daily.universe import BOARDS, BOARD_EXCHANGE, scope_boards



class ExchangePermission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exchange: Literal["SSE", "SZSE", "BSE"]
    enabled: StrictBool
    permission_status: Literal["approved", "unconfirmed"]
    purpose: str = Field(min_length=1)
    permission_basis: str = Field(min_length=1)
    confirmed_on: date
    access_status: Literal["allowed", "access_denied", "rate_limited", "unverified"]
    access_evidence: str = Field(min_length=1)
    llm_export: Literal[False] = False
    commercial_redistribution: Literal[False] = False

    @model_validator(mode="after")
    def permitted(self):
        if self.enabled and self.permission_status != "approved":
            raise ValueError("逐站用途权限未确认")
        return self


class SourcePermission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["baostock", "exchange_lists", "akshare", "tushare"]
    enabled: StrictBool
    permission_status: Literal["approved", "unconfirmed"]
    purpose: str = Field(min_length=1)
    permission_basis: str = Field(min_length=1)
    llm_export: Literal[False] = False
    sites: list[ExchangePermission] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_permission(self):
        if self.enabled and self.permission_status != "approved":
            raise ValueError("未确认用途和权限的来源不能启用")
        if self.enabled and self.provider == "exchange_lists":
            if not self.sites or len({s.exchange for s in self.sites}) != len(self.sites):
                raise ValueError("交易所适配器必须逐站登记用途权限和访问证据，不能重复或为空")
        elif self.enabled and self.provider != "baostock":
            raise ValueError("该来源适配器尚未完成逐站点访问核验，不能仅切换 enabled 启用")
        if self.sites and self.provider != "exchange_lists":
            raise ValueError("逐站登记仅适用于交易所名单")
        return self


class UniverseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["f1-universe-config-v1"]
    config_version: str = Field(default="f1-universe-v1", min_length=1, max_length=128)
    scope: Literal["all_a", "sse_szse_a"]
    required_boards: list[str]
    database: str
    output_directory: str
    calendar_cache: str
    legacy_market_database: str | None = "data/research/market.sqlite3"
    exchange_metadata_cache: str | None = None
    timeout_seconds: int = Field(ge=1, le=60)
    max_attempts: int = Field(ge=1, le=2)
    sources: list[SourcePermission]

    @model_validator(mode="after")
    def complete_scope(self):
        expected = scope_boards(self.scope)
        if len(self.required_boards) != len(expected) or set(self.required_boards) != set(expected):
            raise ValueError("all_a 必须保留全部五类板块，不能因来源失败缩小范围" if self.scope == "all_a"
                             else "sse_szse_a 必须保留沪深全部四类板块")
        if len({s.provider for s in self.sources}) != len(self.sources):
            raise ValueError("来源登记重复")
        for source in self.sources:
            if source.enabled and source.provider == "exchange_lists":
                registered = {site.exchange for site in source.sites}
                required = {BOARD_EXCHANGE[board] for board in expected}
                if not required.issubset(registered):
                    raise ValueError("all_a 交易所适配器必须登记全部三站用途权限和访问证据" if self.scope == "all_a"
                                     else "sse_szse_a 交易所适配器必须登记沪深两站用途权限和访问证据")
        return self


def load_universe_config(project: Path, path: str | Path) -> UniverseConfig:
    config = UniverseConfig.model_validate_json(local_path(project, path).read_text(encoding="utf-8-sig"))
    for name in ("database", "output_directory", "calendar_cache"):
        local_path(project, getattr(config, name))
    if config.legacy_market_database:
        local_path(project, config.legacy_market_database)
    if config.exchange_metadata_cache:
        local_path(project, config.exchange_metadata_cache)
    return config


def _state(value, *, target: date, observed_at: str, evidence_id: str) -> dict:
    return {"value": value, "source": "baostock.query_all_stock.tradeStatus",
            "evidence_id": evidence_id, "verified": type(value) is bool,
            "effective_from": target.isoformat(), "effective_to": target.isoformat(),
            "observed_at": observed_at}


def baostock_discovery_rows(listing: dict, basic: dict, target: date) -> list[dict]:
    """Join dated discovery with descriptive basics, retaining conflicts and unknowns.

    sh/sz are documented provider exchange namespaces, not numerical board rules.
    BaoStock's type=1 means stock, not proof of ordinary RMB A shares or board.
    Missing basic data must not remove a discovered code from the denominator.
    """
    grouped: dict[str, list[dict]] = {}
    if basic.get("ok"):
        for row in basic.get("rows", []):
            grouped.setdefault(row.get("code", ""), []).append(row)
    result = []
    for raw in listing.get("rows", []):
        code = raw.get("code", "")
        matches = grouped.get(code, [])
        meta = matches[0] if len(matches) == 1 else {}
        namespace = code.split(".", 1)[0]
        exchange = {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}.get(namespace, "UNKNOWN")
        # These explicit source types prove nonordinary instruments. Type 1
        # still needs A/B/CDR metadata and cannot be classified by code prefix.
        kind = {"2": "index", "4": "convertible_bond", "5": "etf"}.get(meta.get("type"), "unknown")
        observed = listing["fetched_at"]
        state = raw.get("tradeStatus")
        suspended = state == "0" if state in {"0", "1"} else None
        dates = {}
        for source, field in (("ipoDate", "listing_date"), ("outDate", "delisting_date")):
            try:
                dates[field] = date.fromisoformat(meta.get(source, "")).isoformat()
            except (ValueError, TypeError):
                dates[field] = None
        current_metadata = datetime.fromisoformat(basic.get("fetched_at", observed)).astimezone(SHANGHAI).date() == target
        status = {"0": "delisted", "1": "listed"}.get(meta.get("status"), "unknown") if current_metadata else "unknown"
        result.append({"provider": "baostock", "code": code,
            "name": raw.get("code_name") or meta.get("code_name") or code,
            "exchange": exchange, "board": "unknown", "security_type": kind,
            "metadata_verified": kind != "unknown", "metadata_source": "baostock.query_all_stock+query_stock_basic",
            **dates, "listing_status": status,
            "statuses": {"suspended": _state(suspended, target=target, observed_at=observed,
                evidence_id=listing.get("raw_hash", "unknown"))},
            "raw": {"listing": raw, "basic_matches": matches}})
    return result


def discover_baostock(client, *, target: date, directory: Path, mode: str = "online") -> tuple[list, list, list]:
    requests = []
    listing = client.query("universe", day=target.isoformat())
    atomic_json(directory / "baostock-universe.json", listing)
    requests.append({"operation": "universe", "response": str(directory / "baostock-universe.json"),
        "ok": listing.get("ok", False), "error_code": listing.get("error_code"),
        "status": listing.get("status"), "row_count": len(listing.get("rows", [])),
        "elapsed_seconds": listing.get("elapsed_seconds"), "attempts": listing.get("attempts")})
    # Permission/rate limiting is a source stop, not an invitation to try another query.
    if listing.get("status") in {"permission_denied", "rate_limited"} or not listing.get("ok"):
        basic = {"ok": False, "rows": [], "status": "not_attempted"}
    else:
        basic = client.query("basic_all")
        atomic_json(directory / "baostock-basic-all.json", basic)
        requests.append({"operation": "basic_all", "response": str(directory / "baostock-basic-all.json"),
            "ok": basic.get("ok", False), "error_code": basic.get("error_code"),
            "status": basic.get("status"), "row_count": len(basic.get("rows", [])),
            "elapsed_seconds": basic.get("elapsed_seconds"), "attempts": basic.get("attempts")})
    observed = listing.get("fetched_at") or datetime.now(SHANGHAI).isoformat()
    records = baostock_discovery_rows(listing, basic, target) if listing.get("ok") else []
    errors = ["BaoStock名单缺少普通A股类型与板块元数据；北交所覆盖未经验证"]
    if not listing.get("ok"):
        errors.append("list_request_failed:" + str(listing.get("error_code", "unknown")))
    if not basic.get("ok"):
        errors.append("basic_request_failed_or_not_attempted:" + str(basic.get("error_code", "unknown")))
    # Exhausting SDK pages establishes transport completion only. It is not a
    # trusted all-A denominator or a substitute for five-board reconciliation.
    transport_complete = bool(listing.get("ok") and listing.get("pagination", {}).get("exhausted") is True)
    manifest = {"provider": "baostock", "dataset": "all_stock", "permission_status": "approved",
        "as_of_date": target.isoformat(), "observed_at": observed, "provenance_mode": mode,
        "lineage_id": "baostock", "authoritative": False, "expected_pages": 1,
        "expected_records": None, "complete": transport_complete, "coverage_boards": [], "errors": errors}
    pages = [{"provider": "baostock", "dataset": "all_stock", "page_number": 1,
              "records": records, "terminal": transport_complete}]
    return pages, [manifest], requests


def sync_date(*, project: Path, config_path: str | Path, target: date,
              cutoff_at: str | None = None, now: datetime | None = None,
              client=None, calendar_resolver=None, exchange_transport=None, offline_test: bool = False) -> dict:
    """Caller holds the existing daily lock. Test injection requires isolated mode."""
    from ashare_daily.calendar import resolve_calendar
    from ashare_daily.providers.baostock import BaoStockClient
    from ashare_daily.universe import UniverseStore, sync_universe
    project = Path(project).resolve()
    if (client is not None or calendar_resolver is not None or exchange_transport is not None) and not offline_test:
        raise ValueError("注入服务仅用于显式 offline_test，不能产生真实名单验收")
    config = load_universe_config(project, config_path)
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        raise ValueError("运行时间必须含时区")
    now = now.astimezone(SHANGHAI)
    if target > now.date():
        raise ValueError("目标日期不能在未来")
    cutoff = datetime.fromisoformat(cutoff_at) if cutoff_at else now
    if cutoff.tzinfo is None or cutoff > now or cutoff.astimezone(SHANGHAI).date() < target:
        raise ValueError("资料截点必须含时区，不晚于当前时间且不早于目标日期")
    directory = local_path(project, config.output_directory) / target.isoformat() / (now.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8])
    if offline_test and any("research" in {part.lower() for part in path.parts} for path in
                            (directory, local_path(project, config.database), local_path(project, config.calendar_cache),
                             *([local_path(project, config.exchange_metadata_cache)] if config.exchange_metadata_cache else []))):
        raise ValueError("离线名单测试不能写入 research 路径")
    directory.mkdir(parents=True, exist_ok=False)
    permitted = next((s for s in config.sources if s.provider == "baostock" and s.enabled), None)
    exchanges = next((s for s in config.sources if s.provider == "exchange_lists" and s.enabled), None)
    pages, manifests, requests = [], [], []
    if permitted:
        client = client or BaoStockClient(config.timeout_seconds, config.max_attempts)
        calendar = (calendar_resolver or resolve_calendar)(requested_date=target,
            cache_root=local_path(project, config.calendar_cache), client=client,
            mode="offline_test" if offline_test else "research",
            legacy_database=local_path(project, config.legacy_market_database) if config.legacy_market_database and not offline_test else None)
    else:
        calendar = {"status": "calendar_unverified", "calendar_verified": False,
                    "requested_date": target.isoformat(), "resolved_trade_date": None,
                    "reason": "日历来源尚未获准启用"}
    atomic_json(directory / "calendar.json", calendar)
    source_stopped = calendar.get("response", {}).get("status") in {"permission_denied", "rate_limited"}
    if calendar.get("status") == "verified" and calendar.get("resolved_trade_date") == target.isoformat():
        if exchanges:
            from ashare_daily.providers.exchange_universe import discover_exchange_lists
            pages, manifests, requests = discover_exchange_lists(target=target, directory=directory / "exchange_lists",
                permissions={s.exchange: s.model_dump(mode="json") for s in exchanges.sites},
                timeout_seconds=config.timeout_seconds, mode="offline_test" if offline_test else "online",
                environment_label="local_windows_cli", transport=exchange_transport,
                cache_directory=local_path(project, config.exchange_metadata_cache) if config.exchange_metadata_cache and not offline_test else None,
                scope=config.scope)
        elif permitted and not source_stopped:
            pages, manifests, requests = discover_baostock(client, target=target, directory=directory,
                mode="offline_test" if offline_test else "online")
    completed = datetime.now(SHANGHAI).isoformat() if not offline_test else now.isoformat()
    with UniverseStore(local_path(project, config.database), mode="offline_test" if offline_test else "research") as store:
        snapshot = sync_universe(store, requested_date=target.isoformat(),
            resolved_trade_date=calendar.get("resolved_trade_date"), cutoff_at=cutoff.isoformat(),
            observed_at=completed, pages=pages, manifests=manifests,
            calendar_verified=calendar.get("calendar_verified") is True and calendar.get("status") == "verified",
            scope=config.scope, config_version=config.config_version)
    atomic_json(directory / "snapshot.json", snapshot)
    status = "non_trading_day" if calendar.get("status") == "non_trading_day" else snapshot["status"]
    summary = {k: v for k, v in snapshot.items() if k != "members"}
    summary.update(status=status, requested_date=target.isoformat(), scope=config.scope, capability_stage="f1_discovery_only",
        config_version=config.config_version,
        implementation_stage="F1.1" if exchanges else "F1", started_at=now.isoformat(), completed_at=completed,
        cutoff_at=cutoff.isoformat(), calendar=calendar, source_requests=requests,
        enabled_sources=[s.model_dump(mode="json") for s in config.sources if s.enabled],
        disabled_sources=[s.model_dump() for s in config.sources if not s.enabled],
        snapshot_path=str(directory / "snapshot.json"), run_directory=str(directory),
        config_sha256=hashlib.sha256(local_path(project, config_path).read_bytes()).hexdigest(),
        model_calls=0, market_scanning_status="not_implemented_F2_F3",
        publication_status="not_run", verification_kind="offline_test" if offline_test else "online")
    from ashare_daily.universe_acceptance import write_acceptance_details
    summary.update(write_acceptance_details(directory, snapshot))
    summary["board_coverage"] = {board: {"verified_ordinary_a_count": snapshot["board_counts"][board],
        "market_total": snapshot["board_counts"][board] if snapshot.get("universe_verified") else None,
        "status": "verified" if snapshot.get("universe_verified") else "unverified"} for board in scope_boards(config.scope)}
    if source_stopped:
        summary["source_stop_reason"] = calendar["response"]["status"]
    atomic_json(directory / "result.json", summary)
    return summary
