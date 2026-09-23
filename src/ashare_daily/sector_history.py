"""On-demand versioned history for the immutable sector selection union only."""
from __future__ import annotations

from contextlib import contextmanager, closing
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .market_foundation import F2MarketStore
from .operations.lock import ProcessLock
from .operations.backup import _io
from .operations.paths import resolve_archived_path
from .providers.base import DailyBarRequest, SecurityIdentity
from .providers.baostock import BaoStockClient, SHANGHAI
from .providers.baostock_f2 import resolve_history_calendar
from .providers.sina_history import SinaHistoryProvider, normalize_sina_response
from .sector_selection import digest, verify_selection, confirmed_full_day_halt

PLAN_SCHEMA = "f2s1-history-plan-v1"


def _now():
    return datetime.now(SHANGHAI).isoformat()


def _path(root, value, mode):
    text = str(value)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    raw = Path(text)
    raw = raw if raw.is_absolute() else root / raw
    if any(part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()) for part in (raw, *raw.parents)):
        raise ValueError("history path cannot traverse a symlink/junction")
    path = raw.resolve()
    if not path.is_relative_to(root):
        raise ValueError("history path must remain within project root")
    if mode == "offline_test" and "research" in {part.casefold() for part in path.parts}:
        raise ValueError("offline history cannot write/read research artifacts")
    return path


def _read(path):
    return json.loads(_io(path).read_text(encoding="utf-8-sig"))


def _archive_path(root, value, mode):
    """Read archived locators through a verified restore map; never use for writes."""
    text = str(value)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return _path(root, resolve_archived_path(text, anchor=root), mode)


def _write_new(path, value):
    _io(path.parent).mkdir(parents=True, exist_ok=True)
    body = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    with _io(path).open("xb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(body).hexdigest()


def _seal(value):
    return {**value, "content_hash": digest(value)}


def _verified_file(path):
    value = _read(path)
    if value.get("content_hash") != digest({k: v for k, v in value.items() if k != "content_hash"}):
        raise ValueError("history artifact hash mismatch: " + str(path))
    return value


class _CacheOnlyCalendar(BaoStockClient):
    def query(self, *args, **kwargs):
        raise ValueError("trusted cached calendar required; history preparation does not fetch a calendar")


class _ReadStore(F2MarketStore):
    def __init__(self, path, mode, *, root=None):
        self.path, self.mode = Path(path), mode
        self.project_root = Path(root).resolve() if root is not None else self.path.parent
        self.history_initialized = False
        if self.path.exists():
            with self._connection() as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if any(name.casefold().startswith("demo_") for name in tables):
                    raise ValueError("history market cache provenance mismatch")
                if "market_metadata" in tables:
                    metadata = dict(db.execute("SELECT key,value FROM market_metadata"))
                    if (metadata.get("mode") != "research" or metadata.get("schema_version") != "m1-baostock-market-v1"
                            or metadata.get("verification_kind") != ("live_network" if mode == "research" else "offline_test")):
                        raise ValueError("history legacy market cache provenance mismatch")
                if "f2_schema" not in tables:
                    # A first nonempty sector selection can share an M1 database
                    # before any F2 history has ever been collected. Reading that
                    # legitimate empty cache must not create tables or invent bars.
                    if "market_metadata" not in tables or any(name.startswith("f2_") for name in tables):
                        raise ValueError("history market cache schema missing")
                    return
                if [tuple(row) for row in db.execute("SELECT version,mode FROM f2_schema")] != [(1, mode)]:
                    raise ValueError("history market cache provenance mismatch")
                self.history_initialized = True

    @contextmanager
    def _connection(self):
        if not self.path.is_file():
            raise ValueError("market cache does not exist")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            yield db


def _identity(member):
    if (member.get("metadata_verified") is not True or member.get("security_type") != "ordinary_a"
            or member.get("metadata_conflict") or member.get("discovery_classification") != "ordinary_a"):
        raise ValueError("selected history identity is not verified ordinary A share")
    return SecurityIdentity(member["security_id"], member["code"], member["exchange"], member["board"], metadata_verified=True)


def _inputs(root, selection, config):
    root = Path(root).resolve()
    verify_selection(selection)
    mode = selection.get("mode")
    if mode not in {"research", "offline_test"} or selection["config_hash"] != digest(config):
        raise ValueError("selection/config/provenance differs from frozen input")
    validation = selection.get("purpose") == "engineering_validation"
    if validation:
        if config.get("purpose") != "engineering_validation" or config.get("production_eligible") is not False:
            raise ValueError("engineering history requires isolated purpose config")
        required = {"output_directory": "outputs/engineering_validation/f3s", "database": "data/engineering_validation/f3s/market.sqlite3",
                    "history_lock_file": "data/engineering_validation/f3s/history.lock"}
        if any(_path(root, config.get(key, ""), mode) != root / value for key, value in required.items()):
            raise ValueError("engineering history paths must use isolated namespace")
    elif config.get("purpose", "production") != "production" or config.get("production_eligible", True) is not True:
        raise ValueError("production history cannot use engineering config")
    for member in selection["members"]:
        _identity(member)
        if member.get("provenance_mode") == "offline_test" and mode == "research":
            raise ValueError("research rejects offline member provenance")
    directory = _path(root, config["output_directory"], mode) / selection["selection_id"]
    frozen = directory / "sector_selection.json"
    if frozen.exists() and _read(frozen) != selection:
        raise ValueError("selection differs from archived selection")
    if mode == "research" and not frozen.is_file():
        raise ValueError("research history requires an archived frozen selection")
    database = _path(root, config["database"], mode)
    calendar_path = _path(root, config["calendar_cache"], mode)
    if not validation and any("engineering_validation" in path.parts for path in (directory, database)):
        raise ValueError("production history cannot read engineering stores")
    if config.get("readonly_cache_database"):
        cache = _path(root, config["readonly_cache_database"], mode)
        if not validation or cache == database or "engineering_validation" in cache.parts:
            raise ValueError("invalid readonly engineering cache source")
        if mode == "research" and cache != root / "data/research/market.sqlite3":
            raise ValueError("readonly production cache must be explicitly registered")
    return root, mode, directory, database, calendar_path


def _request(task, adjustment):
    return DailyBarRequest(SecurityIdentity(**task["identity"]), task["expected_dates"][0],
        task["expected_dates"][-1], tuple(task["expected_dates"]), adjustment)


def _numeric(row, *, amount=True):
    # Unknown risk/trading state remains unknown; this is a numerical fact test.
    if row.get("tradestatus") is False:
        return True
    keys = ("open", "high", "low", "close", "volume_shares") + (("amount_cny",) if amount else ())
    allowed = {"missing_preclose"} | ({"missing_amount_cny"} if not amount else set())
    return all(row.get(key) is not None for key in keys) and set(row.get("quality_flags", [])) <= allowed


def _raw_refs(store, member, expected, provider):
    if not store.path.is_file() or not getattr(store, "history_initialized", True) or not expected:
        return {}
    rows = store.read_bars(member["security_id"], expected[0], expected[-1], provider=provider)
    return {row["trade_date"]: {"provider": provider, "fact_hash": row["fact_hash"]}
            for row in rows if row["trade_date"] in expected and row["symbol"] == _identity(member).symbol}


def _aligned(window, refs, expected):
    return bool(window and window["expected_dates"] == expected and
        set(refs) == set(expected) and window.get("raw_fact_hashes") == {day: refs[day]["fact_hash"] for day in expected}
        and {ref["provider"] for ref in refs.values()} == {window["provider"]})


def _window(store, member, expected, provider, refs):
    if not store.path.is_file() or not getattr(store, "history_initialized", True) or not expected:
        return None
    with store._connection() as db:
        candidates = db.execute("SELECT window_id FROM f2_adjustment_windows WHERE security_id=? AND provider=? AND window_start=? AND window_end=? ORDER BY first_seen_at DESC,window_id",
            (member["security_id"], provider, expected[0], expected[-1])).fetchall()
    for row in candidates:
        window = store.get_adjustment_window(row["window_id"])
        if (window["expected_dates"] != expected or window["symbol"] != _identity(member).symbol
                or not window["observations"] or not _aligned(window, refs, expected)):
            continue
        # Cached adjusted artifacts remain tied to their archived source bytes.
        evidence = False
        for observation in window["observations"]:
            path = _archive_path(store.project_root, observation["source_response_path"], store.mode)
            if _io(path).is_file() and hashlib.sha256(_io(path).read_bytes()).hexdigest() == observation["source_file_hash"]:
                evidence = True
                break
        if evidence:
            return window["window_id"]
    return None


def _archived_raw(store, security_id, expected, refs):
    if not store.path.is_file() or not getattr(store, "history_initialized", True) or not expected:
        return None
    with store._connection() as db:
        rows = db.execute("SELECT source_response_path,source_file_hash,quality_json FROM f2_batches WHERE security_id=? AND adjustment_mode='unadjusted' ORDER BY fetched_at DESC,batch_id DESC", (security_id,)).fetchall()
    for row in rows:
        quality = json.loads(row["quality_json"])
        if quality.get("provider") != "sina" or not set(expected) <= set(quality.get("expected_dates", [])):
            continue
        recorded = {record["trade_date"]: digest(record) for record in quality["records"] if record["trade_date"] in expected}
        if recorded != {day: ref["fact_hash"] for day, ref in refs.items()}:
            continue
        source = _archive_path(store.project_root, row["source_response_path"], store.mode)
        if _io(source).is_file() and hashlib.sha256(_io(source).read_bytes()).hexdigest() == row["source_file_hash"]:
            return {"path": str(source), "file_hash": row["source_file_hash"]}
    return None


def _build_plan(root, selection, config, database, calendar_path):
    mode = selection["mode"]
    allowed = selection.get("selection_verified") is True and selection["selection_status"] == "selected"
    base = {"schema_version": PLAN_SCHEMA, "selection_id": selection["selection_id"], "selection_hash": selection["content_hash"],
        "purpose": selection.get("purpose", "production"), "production_eligible": selection.get("production_eligible", True),
        "config_hash": selection["config_hash"], "mode": mode, "market_scope": "sse_szse_a", "research_mode": "sector_first",
        "target_date": selection["target_date"], "cutoff_at": selection["cutoff_at"], "created_at": _now(), "universe_snapshot_id": selection["universe_snapshot_id"],
        "denominator": len(selection["members"]), "tasks": [], "model_calls": 0,
        "outside_selection_policy": "not_requested_by_design", "outside_selection_history_requests": 0,
        "calendar": None, "blockers": [], "physical_source_scope": "Sina returns single-security available whole history; no invented date/offset/limit parameters"}
    if not allowed:
        base["status"] = "no_matching_sectors" if selection["selection_status"] == "no_matching_sectors" and selection.get("selection_verified") else "selection_blocked"
        if selection["members"]:
            base["blockers"].append("unverified_selection_cannot_request_history")
        return _seal(base)
    calendar = resolve_history_calendar(date.fromisoformat(selection["target_date"]), calendar_path,
        history_days=config.get("target_trading_days", 320), client=_CacheOnlyCalendar(), mode=mode)
    base["calendar"] = calendar
    if not calendar["verified"]:
        base.update(status="calendar_blocked", blockers=[calendar.get("validation_error", "trusted_calendar_unavailable")])
        return _seal(base)
    stores = [_ReadStore(database, mode, root=root)]
    if config.get("readonly_cache_database"):
        stores.append(_ReadStore(_path(root, config["readonly_cache_database"], mode), mode, root=root))
    base["allowed_read_databases"] = [str(store.path) for store in stores]
    for member in sorted(selection["members"], key=lambda row: row["security_id"]):
        identity = _identity(member)
        listing = member.get("listing_date")
        listing = date.fromisoformat(listing).isoformat() if listing else None
        expected = [day for day in calendar["trading_dates"] if listing is None or day >= listing]
        task = {"task_id": digest({"selection": selection["selection_id"], "security_id": identity.security_id})[:24],
            "identity": asdict(identity), "name": member.get("name"), "sector_ids": sorted(member["sector_ids"]),
            "listing_date": listing, "risk_states": member.get("statuses", {}), "expected_dates": expected,
            "supplemental_status_evidence": deepcopy(member.get("supplemental_status_evidence", {})),
            "raw_refs": {}, "adjustment_window_id": None, "cached_raw_response": None, "blockers": [],
            "raw_database": str(database), "adjustment_database": str(database),
            "request_reason": "selected_member_missing_history", "status": "pending"}
        if not expected:
            task.update(status="blocked", blockers=["no_trading_dates_in_verified_listing_interval"])
        else:
            for store in stores:
                for provider in ("baostock", "sina"):
                    refs = _raw_refs(store, member, expected, provider)
                    window = _window(store, member, expected, provider, refs)
                    if len(refs) == len(expected) and window:
                        task.update(raw_refs=refs, adjustment_window_id=window, raw_database=str(store.path), adjustment_database=str(store.path),
                            status="cached", request_reason="complete_cached_source_window")
                        break
                    if provider == "sina" and len(refs) > len(task["raw_refs"]):
                        task.update(raw_refs=refs, raw_database=str(store.path), cached_raw_response=_archived_raw(store, identity.security_id, expected, refs))
                if task["status"] == "cached":
                    break
            task["logical_missing_dates"] = sorted(set(expected) - set(task["raw_refs"]))
        base["tasks"].append(task)
    base["status"] = "planned"
    return _seal(base)


def _load_plan(path, selection):
    plan = _verified_file(path)
    if (plan.get("schema_version") != PLAN_SCHEMA or plan.get("selection_hash") != selection["content_hash"]
            or plan.get("config_hash") != selection["config_hash"] or plan.get("denominator") != len(selection["members"])):
        raise ValueError("frozen history plan differs from selection")
    if plan["status"] == "planned" and {task["identity"]["security_id"] for task in plan["tasks"]} != {member["security_id"] for member in selection["members"]}:
        raise ValueError("history plan scope is truncated or expanded")
    return plan


def _checkpoints(directory, plan):
    values = {}
    for task in plan["tasks"]:
        for path in sorted(_io(directory / "history/checkpoints" / task["task_id"]).glob("*.json")):
            record = _verified_file(path)
            if record["selection_hash"] != plan["selection_hash"] or record["task_id"] != task["task_id"]:
                raise ValueError("checkpoint source selection mismatch")
            previous = values.get(task["task_id"])
            if previous is None or (record["sequence"], record["created_at"]) > (previous["sequence"], previous["created_at"]):
                values[task["task_id"]] = record
    return values


def _state(task, checkpoint):
    return deepcopy(checkpoint["state"] if checkpoint else {
        "status": task["status"], "raw_refs": task["raw_refs"], "adjustment_window_id": task["adjustment_window_id"],
        "raw_database": task.get("raw_database"), "adjustment_database": task.get("adjustment_database"),
        "raw_response": task.get("cached_raw_response"), "issues": task["blockers"], "acquisition_complete": task["status"] == "cached"})


def _checkpoint(directory, plan, task, state, sequence, metrics):
    value = _seal({"schema_version": "f2s1-history-checkpoint-v1", "selection_hash": plan["selection_hash"],
        "purpose": plan.get("purpose", "production"), "production_eligible": plan.get("production_eligible", True),
        "task_id": task["task_id"], "sequence": sequence, "created_at": _now(), "state": state, "metrics": metrics})
    _write_new(directory / "history/checkpoints" / task["task_id"] / (str(sequence).zfill(6) + "-" + uuid.uuid4().hex + ".json"), value)
    return value


def _frozen_rows(store, task, refs):
    """Read one security window in one query, retaining exact immutable refs."""
    expected = task["expected_dates"]
    if any(day not in expected for day in refs):
        raise ValueError("checkpoint date outside frozen window")
    if not refs:
        return []
    identity = SecurityIdentity(**task["identity"])
    with store._connection() as db:
        candidates = db.execute("SELECT provider,trade_date,fact_hash,payload_json,first_seen_at FROM f2_bar_versions "
            "WHERE security_id=? AND trade_date BETWEEN ? AND ?", (identity.security_id, expected[0], expected[-1])).fetchall()
    rows, found = [], set()
    for candidate in candidates:
        day = candidate["trade_date"]
        if refs.get(day) != {"provider": candidate["provider"], "fact_hash": candidate["fact_hash"]}:
            continue
        record = json.loads(candidate["payload_json"])
        if (digest(record) != candidate["fact_hash"] or record.get("provider") != candidate["provider"] or
                record.get("security_id") != identity.security_id or record.get("symbol") != identity.symbol or
                record.get("trade_date") != day or record.get("adjustment_mode") != "unadjusted"):
            raise ValueError("frozen history cache reference hash/identity mismatch")
        found.add(day)
        rows.append({**record, "fact_hash": candidate["fact_hash"], "first_seen_at": candidate["first_seen_at"]})
    if found != set(refs):
        raise ValueError("frozen history cache reference missing or mismatched")
    return sorted(rows, key=lambda row: row["trade_date"])


def _state_store(plan, default_store, locator):
    if locator is None or Path(locator) == default_store.path:
        return default_store
    if locator not in plan.get("allowed_read_databases", []):
        raise ValueError("frozen history database not in allowed cache sources")
    path = _archive_path(default_store.project_root, locator, plan["mode"])
    return _ReadStore(path, plan["mode"], root=default_store.project_root)


def _observed_adjusted(store, task, state, checkpoint):
    """Recover a physically complete observed response, with gaps still explicit.

    This is a diagnostic artifact, never a complete store window. The checkpoint
    time bounds candidate observations so later source revisions cannot drift a
    frozen F3 input. Every existing raw fact must match the same source version.
    """
    if (not checkpoint or not store.path.is_file() or not state["raw_refs"]
            or state.get("adjustment_window_id")):
        return None
    with store._connection() as db:
        candidates = db.execute("SELECT * FROM f2_batches WHERE security_id=? AND adjustment_mode='forward_adjusted' "
            "AND fetched_at<=? ORDER BY fetched_at DESC,batch_id DESC",
            (task["identity"]["security_id"], checkpoint["created_at"])).fetchall()
    expected_hashes = {day: ref["fact_hash"] for day, ref in state["raw_refs"].items()}
    for batch in candidates:
        stored = json.loads(batch["quality_json"])
        if (stored.get("provider") != "sina" or stored.get("expected_dates") != task["expected_dates"]
                or stored.get("raw_fact_hashes") != expected_hashes):
            continue
        if {ref["provider"] for ref in state["raw_refs"].values()} != {"sina"}:
            continue
        path = _archive_path(store.project_root, batch["source_response_path"], store.mode)
        if hashlib.sha256(_io(path).read_bytes()).hexdigest() != batch["source_file_hash"]:
            raise ValueError("diagnostic adjusted source hash mismatch")
        response = _read(path)
        quality = normalize_sina_response(response, _request(task, "forward_adjusted"), mode=store.mode)
        if quality != stored or response["raw_hash"] != batch["source_raw_hash"]:
            raise ValueError("diagnostic adjusted normalization/version mismatch")
        if quality["quality_issues"] or quality["factor_missing_dates"] or not quality["missing_dates"]:
            return None
        window = {"security_id": task["identity"]["security_id"], "provider": "sina",
            "symbol": SecurityIdentity(**task["identity"]).symbol, "adjustment_mode": "forward_adjusted",
            "window_start": task["expected_dates"][0], "window_end": task["expected_dates"][-1],
            "expected_dates": list(task["expected_dates"]), "records": quality["records"],
            "anchor_kind": "provider_current_at_fetch", "point_in_time_adjustment_verified": False,
            "adjustment_anchor_hash": quality["adjustment_anchor_hash"], "raw_component_hash": quality["raw_component_hash"],
            "factor_component_hash": quality["factor_component_hash"], "raw_fact_hashes": quality["raw_fact_hashes"],
            "complete": False, "diagnostic_only": True, "missing_dates": quality["missing_dates"],
            "quote_complete": False, "status_complete": False, "research_ready": False,
            "numeric_price_complete": False, "numeric_history_complete": False}
        signature = digest(window)
        window.update(window_id="f2-window-" + signature, content_hash=signature, first_seen_at=batch["fetched_at"],
            observations=[{"batch_id": batch["batch_id"], "fetched_at": batch["fetched_at"],
                "source_response_path": batch["source_response_path"], "source_file_hash": batch["source_file_hash"],
                "source_raw_hash": batch["source_raw_hash"]}])
        return {"window": window, "source_latest_date": quality["source_latest_date"],
            "reference": {"path": str(path), "file_hash": batch["source_file_hash"]}}
    return None


def _supplemental_halt(task, plan):
    """Use only a frozen, dated supplement; keep original risk states intact."""
    evidence = task.get("supplemental_status_evidence", {}).get("suspended")
    if evidence is None:
        return False, None
    if not isinstance(evidence, dict) or not confirmed_full_day_halt({"statuses": {"suspended": evidence}}, plan["target_date"]):
        return False, "supplemental_halt_not_verified_for_target"
    signature = evidence.get("source_file_hash")
    if (evidence.get("source") != "baostock" or not evidence.get("source_path")
            or not isinstance(signature, str) or len(signature) != 64 or any(c not in "0123456789abcdef" for c in signature)):
        return False, "supplemental_halt_source_reference_missing"
    try:
        observed = datetime.fromisoformat(evidence["observed_at"])
        cutoff = datetime.fromisoformat(plan["cutoff_at"])
        if (observed.utcoffset() is None or cutoff.utcoffset() is None or observed > cutoff
                or observed > datetime.now(SHANGHAI) or observed.astimezone(SHANGHAI).date().isoformat() < plan["target_date"]):
            raise ValueError("invalid observation interval")
        reconstruction = observed.astimezone(SHANGHAI).date().isoformat() > plan["target_date"]
        if evidence.get("historical_reconstruction") is not reconstruction:
            raise ValueError("historical reconstruction marker conflicts with observed date")
    except (ValueError, TypeError, KeyError):
        return False, "supplemental_halt_observation_time_unverified"
    return True, None


def _coverage(plan, checkpoints, store):
    details = []
    for task in plan["tasks"]:
        state = _state(task, checkpoints.get(task["task_id"]))
        raw_store = _state_store(plan, store, state.get("raw_database"))
        adjusted_store = _state_store(plan, store, state.get("adjustment_database"))
        rows = _frozen_rows(raw_store, task, state["raw_refs"])
        expected = set(task["expected_dates"])
        missing = sorted(expected - {row["trade_date"] for row in rows})
        numeric = bool(expected) and not missing and all(_numeric(row) for row in rows)
        window = adjusted_store.get_adjustment_window(state["adjustment_window_id"]) if state["adjustment_window_id"] else None
        aligned = bool(_aligned(window, state["raw_refs"], task["expected_dates"]) and window["security_id"] == task["identity"]["security_id"])
        current = next((row for row in rows if row["trade_date"] == plan["target_date"]), None)
        risks = task["risk_states"]
        supplemental_halt, supplemental_issue = _supplemental_halt(task, plan)
        halt = confirmed_full_day_halt({"statuses": risks}, plan["target_date"]) or supplemental_halt
        risk_unknown = any(risks.get(key, {}).get("value") is None for key in ("st", "suspended", "delisting_period"))
        target_numerical = bool(current and not halt and current["tradestatus"] is not False and _numeric(current))
        details.append({"security_id": task["identity"]["security_id"], "code": task["identity"]["code"],
            "board": task["identity"]["board"], "name": task["name"], "sector_ids": task["sector_ids"],
            "task_status": state["status"], "acquisition_complete": state["acquisition_complete"],
            "expected_history_dates": len(expected), "fact_history_dates": len(rows), "history_missing_dates": missing,
            "raw_facts_ready": numeric, "adjustment_ready": aligned, "technical_data_ready": numeric and aligned,
            "valid_target_quote": target_numerical,  # Sector-only numerical alias, not legacy F2 normal-trading verification.
            "target_numerical_observation_ready": target_numerical,
            "quote_coverage_basis": "dated_numerical_facts_unknown_status_retained",
            "target_fact_present": current is not None, "confirmed_suspended": halt,
            "target_quote_expected": not halt, "supplemental_halt_verified": supplemental_halt,
            "supplemental_status_evidence": deepcopy(task.get("supplemental_status_evidence", {})),
            "supplemental_status_issues": [supplemental_issue] if supplemental_issue else [],
            "unknown_trading_status": current is None or current["tradestatus"] is None,
            "risk_unknown": risk_unknown, "research_ready": False, "risk_states": risks,
            "raw_providers": sorted({row["provider"] for row in rows}), "adjustment_provider": window["provider"] if window else None,
            "adjustment_window_id": state["adjustment_window_id"], "issues": state["issues"],
            "field_issues": [{"date": row["trade_date"], "flags": row["quality_flags"]} for row in rows if row["quality_flags"]]})
    ready = sum(row["technical_data_ready"] for row in details)
    status = (plan["status"] if plan["status"] != "planned" else "technical_data_ready_qualification_pending"
              if details and ready == len(details) else "history_partial")
    return {"selection_id": plan["selection_id"], "target_date": plan["target_date"], "status": status,
        "purpose": plan.get("purpose", "production"), "production_eligible": plan.get("production_eligible", True),
        "market_scope": "sse_szse_a", "research_mode": "sector_first", "denominator": plan["denominator"],
        "details": details, "history_ready_count": sum(row["raw_facts_ready"] for row in details),
        "adjustment_ready_count": sum(row["adjustment_ready"] for row in details), "technical_ready_count": ready,
        "risk_unknown_count": sum(row["risk_unknown"] for row in details),
        "confirmed_full_day_halted_count": sum(row["confirmed_suspended"] for row in details),
        "expected_target_quotes": plan["denominator"] - sum(row["confirmed_suspended"] for row in details),
        "valid_target_quotes": sum(row["valid_target_quote"] for row in details),
        "acquisition_complete_count": sum(row["acquisition_complete"] for row in details),
        "pending_count": plan["denominator"] - sum(row["acquisition_complete"] for row in details),
        "blockers": plan["blockers"], "model_calls": 0, "full_market_verified": False, "f3_executed": False,
        "research_ready": False, "outside_selection_history_requests": 0,
        "source_date_notice": "observed history/factor versions; not point-in-time historical selection"}


def report_history(root, selection, config):
    """Read frozen facts/checkpoints only; do not create files, locks or a DB."""
    root, mode, directory, database, _ = _inputs(root, selection, config)
    path = directory / "history/history_fetch_plan.json"
    if not path.is_file():
        latest = directory / "latest_history.json"
        if latest.is_file():
            saved = _verified_file(latest)
            if saved["selection_id"] != selection["selection_id"]:
                raise ValueError("latest history report selection mismatch")
            return {key: value for key, value in saved.items() if key != "content_hash"}
        return {"selection_id": selection["selection_id"], "status": "history_not_prepared", "denominator": len(selection["members"]),
                "details": [], "model_calls": 0, "outside_selection_history_requests": 0}
    plan = _load_plan(path, selection)
    result = _coverage(plan, _checkpoints(directory, plan), _ReadStore(database, mode, root=root))
    result["plan_path"] = str(path)
    latest = directory / "latest_history.json"
    if latest.is_file():
        saved = _verified_file(latest)
        if saved["selection_id"] != selection["selection_id"]:
            raise ValueError("latest history report selection mismatch")
        result.update(metrics=saved["metrics"], run_id=saved["run_id"], report_path=saved["report_path"], stop_reason=saved["stop_reason"])
    result.update(_cumulative(directory, plan))
    return result


def screening_history_inputs(root, selection, config):
    """Export exact frozen source versions for the shared deterministic F3 core."""
    root, mode, directory, database, calendar_path = _inputs(root, selection, config)
    path = directory / "history/history_fetch_plan.json"
    result = {"purpose": selection.get("purpose", "production"), "production_eligible": selection.get("production_eligible", True),
        "selection_id": selection["selection_id"], "securities": {}, "calendar": {}, "file_refs": [], "issues": []}
    if not path.is_file():
        result["issues"] = [] if not selection["members"] else ["history_not_prepared"]
        for member in selection["members"]:
            result["securities"][member["security_id"]] = {"raw_records": [], "adjustment_window": None,
                "expected_dates": [], "issues": ["history_not_prepared"]}
        return result
    plan = _load_plan(path, selection)
    result["calendar"] = deepcopy(plan.get("calendar") or {})
    references = {}
    def verify_ref(locator, signature=None):
        source = _archive_path(root, locator, mode)
        actual = hashlib.sha256(_io(source).read_bytes()).hexdigest()
        if signature is not None and actual != signature:
            raise ValueError("screening source file hash differs from frozen reference")
        references[str(source)] = {"path": str(source), "sha256": actual}
    verify_ref(path)
    checkpoints = _checkpoints(directory, plan)
    for task in plan["tasks"]:
        for checkpoint_path in _io(directory / "history/checkpoints" / task["task_id"]).glob("*.json"):
            verify_ref(checkpoint_path)
    default = _ReadStore(database, mode, root=root)
    for task in plan["tasks"]:
        state = _state(task, checkpoints.get(task["task_id"]))
        raw_store = _state_store(plan, default, state.get("raw_database"))
        adjusted_store = _state_store(plan, default, state.get("adjustment_database"))
        rows = _frozen_rows(raw_store, task, state["raw_refs"])
        if rows:
            with raw_store._connection() as db:
                observations = db.execute("SELECT o.trade_date,o.fact_hash,b.source_response_path,b.source_file_hash,b.provenance_json "
                    "FROM f2_bar_observations o JOIN f2_batches b USING(batch_id) WHERE b.security_id=? AND b.adjustment_mode='unadjusted'",
                    (task["identity"]["security_id"],)).fetchall()
            found, raw_sources = set(), {}
            for observation in observations:
                day = observation["trade_date"]
                if day not in found and state["raw_refs"].get(day, {}).get("fact_hash") == observation["fact_hash"]:
                    provenance = json.loads(observation["provenance_json"])
                    if provenance.get("mode") != mode or provenance.get("provenance_mode") != ("online" if mode == "research" else "offline_test"):
                        raise ValueError("screening raw source provenance differs")
                    locator, signature = observation["source_response_path"], observation["source_file_hash"]
                    if locator in raw_sources and raw_sources[locator] != signature:
                        raise ValueError("screening raw source file has conflicting frozen hashes")
                    raw_sources[locator] = signature
                    found.add(day)
            if found != set(state["raw_refs"]):
                raise ValueError("screening frozen raw observation reference missing")
            # One whole-history response can substantiate hundreds of dates.
            # Keep each fact/provenance check above, but resolve and hash every
            # distinct source only once in this security's frozen export.
            for locator, signature in raw_sources.items():
                verify_ref(locator, signature)
        window = adjusted_store.get_adjustment_window(state["adjustment_window_id"]) if state["adjustment_window_id"] else None
        diagnostic = None
        if window:
            if not _aligned(window, state["raw_refs"], task["expected_dates"]):
                raise ValueError("screening adjusted window is not bound to frozen raw versions")
            if not window["observations"]:
                raise ValueError("screening adjusted source observation missing")
            for observation in window["observations"]:
                verify_ref(observation["source_response_path"], observation["source_file_hash"])
        else:
            diagnostic = _observed_adjusted(adjusted_store, task, state, checkpoints.get(task["task_id"]))
            if diagnostic:
                verify_ref(diagnostic["reference"]["path"], diagnostic["reference"]["file_hash"])
        result["securities"][task["identity"]["security_id"]] = {"raw_records": rows, "adjustment_window": window,
            "diagnostic_adjustment_window": diagnostic["window"] if diagnostic else None,
            "expected_dates": list(task["expected_dates"]),
            "issues": (["history_calendar_dates_missing"] if set(state["raw_refs"]) != set(task["expected_dates"]) else [])
                + (["complete_adjustment_window_missing"] if state["adjustment_window_id"] is None else []),
            "source_limitations": deepcopy(state["issues"]),
            "raw_database": str(raw_store.path), "adjustment_database": str(adjusted_store.path)}
    if set(result["securities"]) != {member["security_id"] for member in selection["members"]}:
        raise ValueError("screening history export did not cover the frozen union")
    result["file_refs"] = [references[key] for key in sorted(references)]
    return result


def _cumulative(directory, plan):
    totals = {key: 0 for key in ("requests", "network_requests", "retries", "response_bytes", "physical_source_rows", "inserted", "updated", "unchanged")}
    fetched = set()
    for task in plan["tasks"]:
        sequences = set()
        for path in _io(directory / "history/checkpoints" / task["task_id"]).glob("*.json"):
            item = _verified_file(path)
            if item["sequence"] in sequences or item["selection_hash"] != plan["selection_hash"]:
                raise ValueError("duplicate or mismatched checkpoint sequence")
            sequences.add(item["sequence"])
            metrics = item["metrics"]
            if metrics.get("requests", 0):
                fetched.add(task["identity"]["security_id"])
            for key in totals:
                totals[key] += metrics.get(key, 0)
    totals["history_requests_outside_selection"] = 0
    return {"cumulative_metrics": totals, "history_fetched_count": len(fetched),
        "cache_reused_count": sum(task["status"] == "cached" for task in plan["tasks"])}


def _make_provider(directory, config, mode):
    return SinaHistoryProvider(directory, permission=config["sina"], mode=mode,
        timeout_seconds=min(15, config.get("timeout_seconds", 15)), pause_seconds=config.get("pause_seconds", 2),
        max_requests=min(2000, config.get("max_history_requests", 2000)))


def prepare_history(root, selection, config, *, dry_run=False, max_seconds=None):
    budget_started = time.monotonic()
    root, mode, directory, database, calendar_path = _inputs(root, selection, config)
    plan_path = directory / "history/history_fetch_plan.json"
    if dry_run:
        plan = _load_plan(plan_path, selection) if plan_path.exists() else _build_plan(root, selection, config, database, calendar_path)
        return {"selection_id": selection["selection_id"], "status": "dry_run", "plan": plan, "denominator": len(selection["members"]),
            "purpose": selection.get("purpose", "production"), "production_eligible": selection.get("production_eligible", True),
            "network_requests": 0, "database_writes": 0, "model_calls": 0, "outside_selection_history_requests": 0}
    limit = config.get("max_seconds", 900) if max_seconds is None else max_seconds
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not 0 <= limit <= 43200:
        raise ValueError("finite history time budget required")
    run_id = uuid.uuid4().hex
    lock_path = _path(root, config.get("history_lock_file", "state/sector_history.lock"), mode)
    with ProcessLock(lock_path, "sector-history-" + run_id):
        plan = _load_plan(plan_path, selection) if plan_path.exists() else _build_plan(root, selection, config, database, calendar_path)
        run_dir = directory / "history/runs" / run_id
        # A failed calendar attempt cannot permanently freeze an empty task list.
        # Preserve the attempt; freeze the canonical plan only once dates verify.
        if plan["status"] == "calendar_blocked":
            plan_path = run_dir / "blocked_plan.json"
        if not plan_path.exists():
            _write_new(plan_path, plan)
        checkpoints = _checkpoints(directory, plan)
        metrics = {"requests": 0, "network_requests": 0, "retries": 0, "response_bytes": 0, "physical_source_rows": 0,
            "source_windows_replayed_with_gaps": 0, "inserted": 0, "updated": 0, "unchanged": 0,
            "history_requests_outside_selection": 0, "history_requests_baselines": 0, "securities_requested": 0,
            "cache_reused_securities": sum(_state(task, checkpoints.get(task["task_id"]))["acquisition_complete"] for task in plan["tasks"])}
        started, source, stop, store = budget_started, None, None, None
        before = database.stat().st_size if database.exists() else 0
        try:
            if plan["status"] == "planned":
                for task in plan["tasks"]:
                    state = _state(task, checkpoints.get(task["task_id"]))
                    if state["acquisition_complete"] or task["blockers"]:
                        continue
                    readonly = _state_store(plan, _ReadStore(database, mode, root=root), state.get("adjustment_database"))
                    observed = _observed_adjusted(readonly, task, state, checkpoints.get(task["task_id"]))
                    if observed is not None and observed["source_latest_date"] >= task["expected_dates"][-1]:
                        # A complete physical response can explicitly omit trading
                        # dates. Replay its verified bytes without inventing rows
                        # or repeatedly downloading the same immutable target.
                        metrics["source_windows_replayed_with_gaps"] += 1
                        continue
                    if time.monotonic() - started >= limit:
                        stop = "time_budget_exhausted"
                        break
                    if source is None:
                        source = _make_provider(run_dir / "http", config, mode)
                        source.client.deadline_monotonic = started + limit
                    sequence = checkpoints.get(task["task_id"], {}).get("sequence", 0)
                    attempted = False
                    for adjustment in ("unadjusted", "forward_adjusted"):
                        if time.monotonic() - started >= limit:
                            stop = "time_budget_exhausted"
                            break
                        request = _request(task, adjustment)
                        if adjustment == "unadjusted" and state["raw_response"]:
                            ref = state["raw_response"]
                            path = _archive_path(root, ref["path"], mode)
                            if not _io(path).is_file() or hashlib.sha256(_io(path).read_bytes()).hexdigest() != ref["file_hash"]:
                                raise ValueError("cached original history response hash mismatch")
                            if source.seed_raw(request, _read(path)):
                                continue
                        if adjustment == "forward_adjusted" and state["adjustment_window_id"]:
                            continue
                        attempt_id = uuid.uuid4().hex
                        _write_new(run_dir / "requests" / (attempt_id + "-started.json"), {
                            "purpose": selection.get("purpose", "production"), "production_eligible": selection.get("production_eligible", True),
                            "selection_id": selection["selection_id"], "task_id": task["task_id"], "request": asdict(request), "started_at": _now()})
                        result = source.fetch_daily_bars(request)
                        attempted = attempted or result.metrics.get("requests", 0) > 0
                        for dest, key in (("requests", "requests"), ("network_requests", "network_requests"), ("retries", "retries"), ("response_bytes", "response_bytes"), ("physical_source_rows", "physical_source_rows")):
                            metrics[dest] += result.metrics.get(key, 0)
                        response_path = run_dir / "responses" / (attempt_id + ".json")
                        file_hash = _write_new(response_path, result.response)
                        sequence += 1
                        if result.response.get("ok") is True:
                            store = store or F2MarketStore(database, mode=mode)
                            store.project_root = root
                            saved = store.save_batch(security_id=request.identity.security_id, symbol=request.identity.symbol,
                                scope="sse_szse_a", universe_snapshot_id=selection["universe_snapshot_id"], response=result.response,
                                source_response_path=response_path, source_response_hash=file_hash, trading_dates=request.expected_dates,
                                provenance_mode="online" if mode == "research" else "offline_test", adjustment_mode=adjustment,
                                provider="sina", request=request)
                            for key in ("inserted", "updated", "unchanged"):
                                result.metrics[key] = saved[key]
                                metrics[key] = metrics.get(key, 0) + saved[key]
                            if adjustment == "unadjusted":
                                state["raw_refs"] = {row["trade_date"]: {"provider": "sina", "fact_hash": digest(row)} for row in saved["quality"]["records"]}
                                state["raw_response"] = {"path": str(response_path), "file_hash": file_hash}
                                state["raw_database"] = str(database)
                            else:
                                state["adjustment_window_id"] = saved["window_id"]
                                state["adjustment_database"] = str(database)
                            state["issues"] = saved["quality"]["quality_issues"] + saved["quality"]["known_gaps"]
                            state["status"] = "facts_saved_with_gaps"
                            adjusted_store = _state_store(plan, store, state.get("adjustment_database"))
                            window = adjusted_store.get_adjustment_window(state["adjustment_window_id"]) if state["adjustment_window_id"] else None
                            state["acquisition_complete"] = _aligned(window, state["raw_refs"], task["expected_dates"])
                        else:
                            state.update(status="pending", issues=[result.response.get("error_code", result.status)])
                            stop = result.status
                        checkpoints[task["task_id"]] = _checkpoint(directory, plan, task, state, sequence, result.metrics)
                        if stop:
                            break
                    metrics["securities_requested"] += int(attempted)
                    # Whole-history HTTP bodies remain immutable on disk. Keep
                    # only the active security in memory during a large union.
                    source.release_security(task["identity"]["security_id"])
                    if stop:
                        break
        except KeyboardInterrupt:
            stop = "interrupted_pending_resume"
        finally:
            if source is not None:
                source.close()
        metrics.update(elapsed_seconds=round(time.monotonic() - started, 6),
            database_growth_bytes=(database.stat().st_size if database.exists() else 0) - before)
        report = _coverage(plan, _checkpoints(directory, plan), _ReadStore(database, mode, root=root))
        report.update(run_id=run_id, completed_at=_now(), metrics=metrics, stop_reason=stop, plan_path=str(plan_path),
            report_path=str(run_dir / "readiness_history.json"), resume_selection_id=selection["selection_id"])
        report.update(_cumulative(directory, plan))
        _write_new(run_dir / "readiness_history.json", _seal(report))
        latest = directory / "latest_history.json"
        temporary = directory / (".latest_history-" + run_id + ".json")
        _write_new(temporary, _seal(report))
        os.replace(temporary, latest)
        return report
