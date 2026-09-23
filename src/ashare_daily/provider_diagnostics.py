"""Opt-in, bounded provider checks with immutable evidence and no market writes."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .calendar import _digest, _verified_rows
from .market_pipeline import _path, _provider_policy, load_market_config
from .providers.base import BOARD_EXCHANGES, DailyBarRequest, MarketDataProvider, SecurityIdentity, iso_date
from .providers.baostock import SHANGHAI
from .universe import UniverseStore, scope_boards
from .universe_service import load_universe_config


def _now():
    return datetime.now(SHANGHAI).isoformat()


def _permission_issue(exc) -> bool:
    message = str(exc)
    return "permission" in message.lower() or "权限" in message or "许可" in message


def _write_new(path: Path, value: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(body)
    return hashlib.sha256(body).hexdigest()


def _read_snapshot(root: Path, config: dict, target: str, universe_config) -> dict:
    """Read an existing research ledger; never invoke its mutating constructor."""
    if universe_config.scope != config["scope"]:
        raise ValueError("market_universe_scope_mismatch")
    path = _path(root, universe_config.database)
    if not path.is_file():
        raise ValueError("universe_database_missing")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        schema = connection.execute("SELECT version,mode FROM f1_schema").fetchall()
        if len(schema) != 1 or tuple(schema[0]) != (1, "research"):
            raise ValueError("universe_provenance_mode_mismatch")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if any(name.casefold().startswith("demo_") for name in tables):
            raise ValueError("universe_demo_database_rejected")
        if "market_metadata" in tables:
            metadata = dict(connection.execute("SELECT key,value FROM market_metadata"))
            if metadata.get("mode") != "research" or metadata.get("verification_kind") != "live_network":
                raise ValueError("universe_legacy_provenance_unverified")
        row = connection.execute("SELECT * FROM f1_universe_snapshots WHERE requested_date=? "
            "AND COALESCE(json_extract(payload,'$.scope'),'all_a')=? "
            "ORDER BY first_observed_at DESC,rowid DESC LIMIT 1", (target, config["scope"])).fetchone()
        if row is None:
            raise ValueError("target_universe_snapshot_missing")
        snapshot = UniverseStore._decode_snapshot(row)
    _select_requests(snapshot, target, config["scope"])
    return snapshot


def _select_requests(snapshot: dict, target: str, scope: str) -> list[DailyBarRequest]:
    """Pure identity validation. This helper grants no online client injection."""
    if scope != "sse_szse_a":
        raise ValueError("unsupported_scope")
    if snapshot.get("mode") != "research" or snapshot.get("scope") != scope:
        raise ValueError("universe_provenance_or_scope_mismatch")
    if snapshot.get("requested_date") != target or snapshot.get("resolved_trade_date") != target:
        raise ValueError("universe_target_date_mismatch")
    if any(snapshot.get(key) is not True for key in ("universe_verified", "collection_ready", "calendar_verified")):
        raise ValueError("universe_not_verified")
    if tuple(snapshot.get("required_boards", [])) != scope_boards(scope) or snapshot.get("blockers"):
        raise ValueError("universe_boards_or_blockers_invalid")
    observed, cutoff = (datetime.fromisoformat(snapshot[key]) for key in ("observed_at", "cutoff_at"))
    if observed.utcoffset() is None or cutoff.utcoffset() is None or observed < cutoff or observed > datetime.now(SHANGHAI):
        raise ValueError("universe_observation_time_invalid")
    manifests = snapshot.get("source_manifests", [])
    if not manifests:
        raise ValueError("universe_source_manifest_missing")
    if any(m.get("permission_status") != "approved" for m in manifests):
        raise ValueError("universe_source_permission_required")
    if any(m.get("provenance_mode") != "online" for m in manifests):
        raise ValueError("universe_source_provenance_unverified")
    if any(m.get("complete") is not True for m in manifests):
        raise ValueError("universe_source_completeness_unverified")
    members = [m for m in snapshot["members"] if m.get("discovery_classification") == "ordinary_a"]
    counts = Counter(m.get("board") for m in members)
    if (len(members) != snapshot.get("ordinary_a_count") or len({m["security_id"] for m in members}) != len(members)
            or dict(counts) != snapshot.get("board_counts") or set(counts) != set(BOARD_EXCHANGES)):
        raise ValueError("universe_identity_or_board_counts_mismatch")
    identities = []
    for member in members:
        if member.get("metadata_verified") is not True or member.get("security_type") != "ordinary_a" or member.get("metadata_conflict"):
            raise ValueError("universe_member_metadata_unverified")
        if member.get("provenance_mode") == "offline_test":
            raise ValueError("universe_test_member_rejected")
        identity = SecurityIdentity(member["security_id"], member["code"], member["exchange"], member["board"],
                                    scope=scope, metadata_verified=True)
        identities.append(identity)
    return [DailyBarRequest(next(i for i in sorted(identities, key=lambda i: (i.code, i.security_id)) if i.board == board),
                            target, target, (target,), "unadjusted") for board in scope_boards(scope)]


def _permissions_for_inputs(universe_config) -> None:
    sources = {source.provider: source for source in universe_config.sources}
    for name in ("baostock", "exchange_lists"):
        source = sources.get(name)
        if source is None or not source.enabled or source.permission_status != "approved":
            raise ValueError("universe_calendar_permission_required")
    sites = {site.exchange: site for site in sources["exchange_lists"].sites}
    if any(exchange not in sites or not sites[exchange].enabled or sites[exchange].permission_status != "approved"
           for exchange in ("SSE", "SZSE")):
        raise ValueError("universe_site_permission_required")


def _cached_calendar(root: Path, directories: list[str], target: str) -> dict:
    candidates, rejections = [], []
    for directory in dict.fromkeys(directories):
        for path in sorted(_path(root, directory).glob("*.json")):
            try:
                path = _path(root, path)
                packet = json.loads(path.read_text(encoding="utf-8-sig"))
                body = {key: value for key, value in packet.items() if key != "content_hash"}
                if (packet.get("schema_version") != "f1-calendar-cache-v1" or packet.get("mode") != "research"
                        or packet.get("provider") != "baostock" or packet.get("content_hash") != _digest(body)):
                    raise ValueError("calendar_cache_hash_or_provenance_invalid")
                response = packet["response"]
                if packet.get("first_seen_at") != response.get("fetched_at") or any(
                    key in response and response[key] not in ("online", "live_network")
                    for key in ("provenance_mode", "verification_kind")):
                    raise ValueError("calendar_response_provenance_invalid")
                days = _verified_rows(response)
                day = iso_date(target)
                if day in days:
                    candidates.append((response["fetched_at"], days[day], path, response))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                rejections.append({"path": str(path), "reason": str(exc)})
    if not candidates:
        return {"calendar_verified": False, "calendar": {}, "cache_rejections": rejections}
    latest = max(datetime.fromisoformat(item[0]) for item in candidates)
    selected = [item for item in candidates if datetime.fromisoformat(item[0]) == latest]
    if len({item[1] for item in selected}) != 1:
        return {"calendar_verified": False, "calendar": {}, "cache_rejections": rejections,
                "reason": "calendar_cache_conflict"}
    stamp, opened, path, response = selected[0]
    return {"calendar_verified": True, "calendar": {target: opened}, "cache_rejections": rejections,
            "cached": True, "source_path": str(path), "source_raw_hash": response["raw_hash"],
            "source_file_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "fetched_at": stamp}


class _RecordingProvider(MarketDataProvider):
    def __init__(self, source, record):
        self.source, self.name, self.record = source, source.name, record

    def fetch_daily_bars(self, request):
        result = self.source.fetch_daily_bars(request)
        self.record(result.provider, result.response, result.provenance(), result.metrics)
        return result


def _construct_source(config: dict, provider: str):
    # This function has no test-mode or transport/client injection option.
    if provider == "baostock":
        from .providers.baostock_f2 import BaoStockF2Client
        from .providers.baostock_provider import BaoStockProvider
        client = BaoStockF2Client(timeout_seconds=config["timeout_seconds"], max_attempts=config["max_attempts"],
                                 pause_seconds=config["pause_seconds"])
        return BaoStockProvider(client, mode="research", owns_client=True)
    from .providers.eastmoney import EastMoneyProvider
    return EastMoneyProvider(permission=_provider_policy(config)["eastmoney"], mode="research",
                             timeout_seconds=min(15, config["timeout_seconds"]),
                             max_attempts=config["max_attempts"], pause_seconds=config["pause_seconds"])


def _close_sources(*sources) -> list[dict]:
    failures, closed = [], set()
    for source in sources:
        if source is None or id(source) in closed:
            continue
        closed.add(id(source))
        try:
            source.close()
        except (OSError, ValueError, RuntimeError) as exc:
            failures.append({"provider": source.name, "reason": str(exc)})
    return failures


def provider_check(*, project_root, config_path, provider, target_date, online=False) -> tuple[dict, int]:
    """Default is config-only; --online is bounded and always research provenance."""
    root, target = Path(project_root).resolve(), iso_date(target_date)
    if provider not in {"baostock", "eastmoney"} or type(online) is not bool:
        raise ValueError("explicit provider and boolean online flag required")
    identifier = uuid.uuid4().hex
    directory = _path(root, "outputs/verification/provider_checks/" + target.isoformat() + "/" + identifier)
    result = {"schema_version": "provider-check-v1", "provider": provider, "target_date": target.isoformat(),
              "check_id": identifier, "mode": "research", "online_requested": online, "started_at": _now(),
              "status": "not_checked", "reason": "online_not_requested", "network_requests": 0,
              "online_quote_verified": False, "online_daily_bar_verified": False,
              "full_market_verified": False, "sample_only": True, "model_calls": 0,
              "source_business_date": None, "source_responses": [], "result_path": str(directory / "result.json")}

    def finish(code):
        result["completed_at"], result["exit_code"] = _now(), code
        _write_new(directory / "result.json", result)
        return result, code

    def record(name, response, provenance=None, metrics=None):
        path = directory / "responses" / (str(len(result["source_responses"]) + 1).zfill(2) + "-" + name + ".json")
        digest = _write_new(path, response)
        attempts = response.get("attempts", [])
        count = metrics.get("requests", 0) if isinstance(metrics, dict) else len(attempts) if isinstance(attempts, list) else 0
        if type(count) is not int or count < 0:
            raise ValueError("invalid_provider_request_metrics")
        result["network_requests"] += count
        result["source_responses"].append({"provider": name, "path": str(path), "source_file_hash": digest,
            "source_raw_hash": response.get("raw_hash"), "operation": response.get("operation"),
            "status": response.get("status"), "error_code": response.get("error_code"),
            "requests": count, "provenance": provenance})

    try:
        config = load_market_config(root, config_path)
        policy = _provider_policy(config)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result.update(status="unavailable", reason="permission_required" if _permission_issue(exc) else "invalid_config",
                      detail=str(exc))
        return finish(2)
    result.update(scope=config["scope"], config_version=config["config_version"])
    if provider == "eastmoney" and (not policy or policy["eastmoney"].get("enabled") is not True
            or policy["eastmoney"].get("permission_status") != "approved"
            or policy["eastmoney"].get("purpose") != "personal_noncommercial_local_research"
            or policy["eastmoney"].get("permitted_automated_access") is not True
            or policy["eastmoney"].get("permitted_storage") is not True):
        result.update(status="unavailable", reason="permission_required")
        return finish(2)
    if not online:
        return finish(0)
    if config["scope"] != "sse_szse_a":
        result.update(status="unavailable", reason="unsupported_scope")
        return finish(2)
    if target > datetime.now(SHANGHAI).date():
        result.update(status="unavailable", reason="future_target_date")
        return finish(2)
    try:
        universe_config = load_universe_config(root, config["universe_config"])
        _permissions_for_inputs(universe_config)
        snapshot = _read_snapshot(root, config, target.isoformat(), universe_config)
        requests = _select_requests(snapshot, target.isoformat(), config["scope"])
        result["universe"] = {"snapshot_id": snapshot["snapshot_id"], "content_hash": snapshot["content_hash"],
            "requested_date": snapshot["requested_date"], "observed_at": snapshot["observed_at"],
            "historical_reconstruction": snapshot.get("historical_reconstruction"),
            "board_counts": snapshot["board_counts"], "universe_verified": True}
        result["requests"] = [asdict(request) for request in requests]
        calendar = _cached_calendar(root, [config["calendar_cache"], universe_config.calendar_cache], target.isoformat())
        result["calendar"] = calendar
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        result.update(status="unavailable", reason="permission_required" if _permission_issue(exc) else "universe_unverified",
                      detail=str(exc))
        return finish(2)
    if calendar.get("reason") == "calendar_cache_conflict":
        result.update(status="unavailable", reason="calendar_unverified")
        return finish(2)
    source = calendar_source = None
    try:
        if calendar["calendar_verified"] is not True:
            calendar_source = _construct_source(config, "baostock")
            checked = calendar_source.fetch_trading_calendar(start_date=target.isoformat(), end_date=target.isoformat())
            record("baostock", checked.response, checked.provenance(), checked.metrics)
            calendar = {**checked.quality, "cached": False, "status": checked.status}
            result["calendar"] = calendar
        if calendar.get("calendar_verified") is not True:
            result.update(status="unavailable", reason="calendar_unverified")
        elif calendar.get("calendar", {}).get(target.isoformat()) is not True:
            result.update(status="unavailable", reason="non_trading_day")
        else:
            source = calendar_source if provider == "baostock" and calendar_source is not None else _construct_source(config, provider)
            health = _RecordingProvider(source, record).healthcheck(requests)
            result.update(status=health["status"], reason="sample_passed" if health["status"] == "healthy" else "sample_incomplete",
                          healthcheck=health, online_daily_bar_verified=health["status"] == "healthy",
                          online_quote_verified=health["status"] == "healthy")
    except (ValueError, KeyError, TypeError, OSError, ImportError) as exc:
        result.update(status="unavailable", reason="provider_check_failed", detail=str(exc))
    finally:
        close_errors = _close_sources(source, calendar_source)
        if close_errors:
            result.update(close_errors=close_errors, status="unavailable", reason="provider_close_failed",
                          online_quote_verified=False, online_daily_bar_verified=False)
    return finish(0 if result["status"] == "healthy" else 2)
