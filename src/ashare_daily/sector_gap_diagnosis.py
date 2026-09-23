"""Versioned, selected-only history gap diagnosis; never patches market prices."""
from __future__ import annotations

from copy import deepcopy
import csv
from datetime import datetime
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import time
import uuid

from .market_foundation import normalize_baostock_rows
from .providers.baostock import BaoStockClient, SHANGHAI, validate_request
from .providers.baostock_f2 import BaoStockF2Client
from .providers.sector_status import _permission
from .providers.sina_history import _history_rows, _date, normalize_sina_response, parse_factors
from .sector_history import (_inputs, _archive_path, _io, _read, _request, _load_plan,
    _write_new, _seal, _verified_file, screening_history_inputs)
from .sector_selection import digest

SCHEMA = "f4s1-history-gap-diagnosis-v1"
ALLOWED_CODES = frozenset({"000609", "600673"})  # Explicit task scope, never a production universe.
ENDPOINT = "baostock://public-api.baostock.com:10030/query_history_k_data_plus"


def _now():
    return datetime.now(SHANGHAI).isoformat()


def _reference(root, locator, mode, refs, checksum=None):
    path = _archive_path(root, locator, mode)
    actual = hashlib.sha256(_io(path).read_bytes()).hexdigest()
    if checksum is not None and actual != checksum:
        raise ValueError("gap evidence hash mismatch")
    refs[str(path)] = {"path": str(path), "sha256": actual}
    return path


def _group_dates(missing, calendar):
    """Only join consecutive verified trading dates, never invent weekdays."""
    positions = {day: index for index, day in enumerate(calendar)}
    if len(missing) != len(set(missing)) or any(day not in positions for day in missing):
        raise ValueError("gap dates must be unique verified trading dates")
    groups = []
    for day in sorted(missing):
        if groups and positions[day] == positions[groups[-1][-1]] + 1:
            groups[-1].append(day)
        else:
            groups.append([day])
    return groups


def _ledger(root, selection, config):
    root, mode, directory, _, _ = _inputs(root, selection, config)
    if selection.get("purpose") != "engineering_validation" or selection.get("production_eligible") is not False:
        raise ValueError("gap diagnosis requires explicit isolated engineering selection")
    plan_path = directory / "history/history_fetch_plan.json"
    plan = _load_plan(plan_path, selection)
    history = screening_history_inputs(root, selection, config)
    calendar = history["calendar"]
    if calendar.get("verified") is not True or not calendar.get("trading_dates"):
        raise ValueError("gap diagnosis requires verified frozen calendar")
    dates = calendar["trading_dates"]
    if dates[-1] != selection["target_date"]:
        raise ValueError("gap calendar does not end at target")
    from .sector_screening import validate_screening_config
    strategy_path = _archive_path(root, "config/sector_screening.json", mode)
    strategy = validate_screening_config(_read(strategy_path))
    lengths = {"valid_history": strategy["min_history_trading_days"], "ma_short": strategy["ma_short_days"],
        "ma_long": strategy["ma_long_days"], "amount": strategy["amount_days"], "relative_return": strategy["return_days"] + 1}
    requirements = {key: dates[-length:] for key, length in lengths.items()}
    refs = {}
    for ref in history["file_refs"]:
        _reference(root, ref["path"], mode, refs, ref["sha256"])
    _reference(root, plan_path, mode, refs)
    _reference(root, strategy_path, mode, refs)
    members = {row["security_id"]: row for row in selection["members"]}
    rows, securities, tasks = [], [], []
    for task in plan["tasks"]:
        identity = task["identity"]
        packet = history["securities"][identity["security_id"]]
        raw_dates = {row["trade_date"] for row in packet["raw_records"]}
        missing = sorted(set(task["expected_dates"]) - raw_dates)
        if not missing:
            continue
        if identity["code"] not in ALLOWED_CODES:
            raise ValueError("additional missing security outside explicitly approved gap diagnosis scope")
        window = packet.get("diagnostic_adjustment_window") or packet.get("adjustment_window")
        if not window or not window.get("observations"):
            raise ValueError("gap diagnosis needs the archived raw/factor response, not a synthesized window")
        observation = window["observations"][0]
        source = _reference(root, observation["source_response_path"], mode, refs, observation["source_file_hash"])
        response = _read(source)
        request = _request(task, "forward_adjusted")
        quality = normalize_sina_response(response, request, mode=mode)
        if quality["raw_fact_hashes"] != {r["trade_date"]: r["fact_hash"] for r in packet["raw_records"]}:
            raise ValueError("gap source raw versions differ from frozen history")
        decoded = _history_rows(response["raw_history"], request, mode)
        source_dates = [_date(r["date"]) for r in decoded]
        adjusted_dates = {r["trade_date"] for r in quality["records"]}
        factors = parse_factors(response["factor_table"], request.identity, mode)
        member = members[identity["security_id"]]
        security = {"identity": deepcopy(identity), "name": member.get("name"), "target_date": selection["target_date"],
            "request_start": task["expected_dates"][0], "request_end": task["expected_dates"][-1],
            "source_first_date": min(source_dates), "source_latest_date": max(source_dates), "physical_source_rows": len(decoded),
            "source_duplicate_dates": len(source_dates) - len(set(source_dates)), "normalized_rows": len(quality["records"]),
            "normalization_issues": quality["quality_issues"], "factor_total": len(factors), "missing_dates": missing,
            "listing_date": member.get("listing_date"), "delisting_date": member.get("delisting_date"),
            "listing_metadata_source": member.get("metadata_source"), "listing_evidence_id": member.get("evidence_id"),
            "source_response_path": str(source), "source_file_hash": observation["source_file_hash"],
            "source_raw_hash": response["raw_hash"], "raw_component_hash": quality["raw_component_hash"],
            "factor_component_hash": quality["factor_component_hash"], "source_fetched_at": response["fetched_at"],
            "physical_response_scope": quality["physical_response_scope"], "source_row_limit": quality["source_row_limit"],
            "pagination": "single_complete_http_body_local_decode_no_pagination_or_date_slice_parameters",
            "history_window_accounted_for": False, "cache_target_complete": False}
        securities.append(security)
        for day in missing:
            eligible_factors = [f for f in factors if f["date"] <= day]
            in_source = day in source_dates
            rows.append({"security_id": identity["security_id"], "code": identity["code"], "symbol": request.identity.symbol,
                "target_date": selection["target_date"], "date": day, "is_verified_trading_day": calendar["calendar"].get(day) is True,
                "in_verified_listing_interval": bool(member.get("metadata_verified") and member.get("listing_date") and day >= member["listing_date"]
                    and (not member.get("delisting_date") or day <= member["delisting_date"])),
                "raw_record_present": day in raw_dates, "sina_decoded_record_present": in_source,
                "adjusted_record_present": day in adjusted_dates, "factor_present": bool(eligible_factors),
                "factor_effective_date": eligible_factors[-1]["date"] if eligible_factors else None,
                "within_rule_windows": {key: day in value for key, value in requirements.items()},
                "normalization_issues": [item for item in quality["quality_issues"] if item.get("date") == day],
                "source_id": "sina", "source_response_path": str(source), "source_file_hash": observation["source_file_hash"],
                "source_fetched_at": response["fetched_at"], "historical_state": None,
                "classification": "normalization_or_source_record_issue" if in_source else "source_omitted_date_reason_unknown",
                "reason": "decoded_original_body_has_no_record" if not in_source else "source_row_did_not_produce_a_frozen_fact",
                "definitive_non_trading": False, "price_rows_inserted": 0, "adjustment_prices_patched": False,
                "can_fill_from_current_evidence": False, "blocks_original_valid_history_window": day in requirements["valid_history"]})
        for group in _group_dates(missing, dates):
            if len(group) > 9:
                raise ValueError("targeted gap request exceeds approved nine-trading-date window")
            tasks.append({"security_id": identity["security_id"], "code": identity["code"], "expected_dates": group,
                "parameters": {"code": request.identity.symbol, "start_date": group[0], "end_date": group[-1],
                    "security_type": "stock", "adjustment_mode": "unadjusted"}})
    if len(tasks) > 4 or len(rows) > 14:
        raise ValueError("gap diagnostic request scope exceeds explicit task budget")
    return directory, rows, securities, tasks, requirements, refs


def _validate_response(response, task, mode):
    params = validate_request("history_f2", response.get("parameters", {}))
    if params != task["parameters"] or not BaoStockClient._valid_worker_result(response, "history_f2", params) or response.get("ok") is not True:
        raise ValueError("gap SDK identity/request/protocol failure")
    observed = datetime.fromisoformat(response["fetched_at"])
    if observed.utcoffset() is None or observed > datetime.now(SHANGHAI) or observed.date().isoformat() < params["end_date"]:
        raise ValueError("gap observation timestamp invalid")
    for key, expected in (("mode", mode), ("provenance_mode", "online" if mode == "research" else "offline_test"),
            ("verification_kind", "live_network" if mode == "research" else "offline_test")):
        if key in response and response[key] != expected:
            raise ValueError("gap response provenance conflict")
    if mode == "offline_test" and response.get("verification_kind") != "offline_test":
        raise ValueError("offline gap response must explicitly identify test data")
    diagnostics = response.get("diagnostics", {})
    events = diagnostics.get("events", [])
    queried = [e for e in events if e.get("stage") == "query_wait" and e.get("state") == "completed"]
    ended = [e for e in events if e.get("stage") == "row_read" and e.get("state") == "completed"]
    rows = response["rows"]
    if (diagnostics.get("failure_stage") is not None or diagnostics.get("hard_timeout") is not False
            or len(queried) != 1 or queried[0].get("error_code") != "0" or queried[0].get("initial_page") not in (1, "1")
            or queried[0].get("initial_page_records") != len(rows) or len(ended) != 1 or ended[0].get("row_count") != len(rows)):
        raise ValueError("gap SDK reader termination not verified")
    if (len({r["date"] for r in rows}) != len(rows) or any(r["code"] != params["code"] or r["date"] not in task["expected_dates"]
            or r.get("adjustflag") != "3" for r in rows)):
        raise ValueError("gap SDK duplicate/outside date or symbol mismatch")
    quality = normalize_baostock_rows(rows, security_id=task["security_id"], symbol=params["code"],
        start_date=params["start_date"], end_date=params["end_date"], trading_dates=task["expected_dates"], adjustment_mode="unadjusted")
    return {r["date"]: r for r in rows}, quality


def _apply_response(rows, task, response, reference, *, mode):
    indexed, quality = _validate_response(response, task, mode)
    for item in rows:
        if item["security_id"] != task["security_id"] or item["date"] not in task["expected_dates"]:
            continue
        source = indexed.get(item["date"])
        item["cross_source"] = {"source_id": "baostock", **reference, "source_raw_hash": response["raw_hash"],
            "observed_at": response["fetched_at"], "fetched_at": response["fetched_at"], "published_at": None,
            "source_business_date": source["date"] if source else None, "record_present": source is not None,
            "reader_end_verified": True, "field_units": {"price": "CNY", "volume": "shares", "amount": "CNY"}}
        if source is None:
            item.update(reason="both_verified_responses_omit_date_state_unknown")
            continue
        status = source.get("tradestatus")
        halt = status == "0"
        # A missing volume/amount is not a positive-activity contradiction and
        # must remain missing. The explicit dated tradestatus field supplies
        # the halt fact; neither zero nor an empty activity field implies it.
        activity = [Decimal(source[k]) for k in ("volume", "amount") if source.get(k) not in (None, "")]
        prices = [Decimal(source[k]) for k in ("open", "high", "low", "close") if source.get(k) not in (None, "")]
        semantic = not halt or (all(n.is_finite() and n == 0 for n in activity) and len(set(prices)) <= 1)
        item["source_placeholder_activity"] = {"volume_raw": source.get("volume"), "amount_raw": source.get("amount"),
            "missing_fields": [k for k in ("volume", "amount") if source.get(k) in (None, "")],
            "filled_values": False, "status_basis": "explicit_tradestatus_not_activity_inference"}
        item["historical_state"] = {"value": True if halt and semantic else False if status == "1" else None,
            "verified": semantic and status in {"0", "1"}, "full_day": halt and semantic,
            "field": "suspended", "target_date": item["date"], "effective_from": item["date"], "effective_to": item["date"],
            "source_id": "baostock", "evidence_id": "gap-status-" + digest({"source": response["raw_hash"], "date": item["date"]})[:24],
            "published_at": None, "observed_at": response["fetched_at"], "fetched_at": response["fetched_at"],
            "source_business_date": source["date"], "historical_reconstruction": True,
            "completeness_scope": "one_explicit_daily_stock_row", "conflict": not semantic,
            "source_path": reference["path"], "source_file_hash": reference["sha256"], "source_raw_hash": response["raw_hash"],
            "raw_tradestatus": status, "reason": "provider_explicit_daily_status"}
        if halt and semantic:
            item.update(classification="verified_non_trading_date", definitive_non_trading=True,
                reason="BaoStock_explicit_daily_halt_placeholder_Sina_omits_row_activity_nulls_preserved")
        elif status == "1":
            item.update(classification="source_disagreement_real_quote_available", reason="BaoStock_traded_row_exists_Sina_omits_date",
                can_fill_from_current_evidence=False)
        else:
            item.update(classification="source_status_conflict" if not semantic else "source_status_unknown", reason="cannot_confirm_historical_non_trading")
    return quality


def diagnose_history_gaps(root, selection, config, *, online=False, max_seconds=120, source_revision=None):
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or not 0 < max_seconds <= 120:
        raise ValueError("gap diagnosis requires bounded 0..120 seconds")
    root = Path(root).resolve()
    mode = selection.get("mode")
    if online and mode != "research":
        raise ValueError("online gap diagnosis rejects test provenance")
    if online and source_revision is not None:
        raise ValueError("source revision replay and new network observation are separate runs")
    started = time.monotonic()
    directory, rows, securities, tasks, requirements, refs = _ledger(root, selection, config)
    observed = _now()
    revision = "gaps-" + datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid.uuid4().hex[:12]
    output = directory / "f4s1/gap_diagnosis" / revision
    manifest = {"schema_version": SCHEMA, "mode": mode, "purpose": "engineering_validation", "production_eligible": False,
        "selection_id": selection["selection_id"], "selection_content_hash": selection["content_hash"],
        "target_date": selection["target_date"], "source_cutoff_at": selection["cutoff_at"], "observed_at": observed,
        "revision_run_id": revision, "online_requested": online, "source_revision": str(source_revision) if source_revision else None,
        "max_seconds": max_seconds, "max_requests": 4,
        "max_attempts": 1, "tasks": tasks, "file_refs": list(refs.values()), "model_calls": 0, "database_writes": 0,
        "physical_request_scope": "verified_missing_trading_date_groups_only_no_other_security_history"}
    manifest_hash = _write_new(output / "manifest.json", _seal(manifest))
    refs[str(output / "manifest.json")] = {"path": str(output / "manifest.json"), "sha256": manifest_hash}
    metrics = {"requests": 0, "network_requests": 0, "retries": 0, "source_rows": 0, "response_bytes": 0,
        "price_rows_inserted": 0, "other_selected_security_history_requests": 0, "outside_selection_requests": 0}
    requests, stop = [], None
    replayed = False
    if source_revision is not None:
        source_path = _reference(root, source_revision, mode, refs)
        if source_path.parent.parent != directory / "f4s1/gap_diagnosis" or source_path.name != "history_gap_diagnosis.json":
            raise ValueError("gap source revision must belong to the same isolated selection")
        previous = _verified_file(source_path)
        if any(previous.get(k) != v for k, v in (("schema_version", SCHEMA), ("mode", mode),
                ("selection_id", selection["selection_id"]), ("selection_content_hash", selection["content_hash"]),
                ("purpose", "engineering_validation"), ("production_eligible", False), ("target_date", selection["target_date"]))):
            raise ValueError("gap source revision identity/purpose mismatch")
        _permission(root) if mode == "research" else None
        for ref in previous["file_refs"]:
            _reference(root, ref["path"], mode, refs, ref["sha256"])
        seen = set()
        for original in previous["requests"]:
            matching = [t for t in tasks if t == original["task"]]
            key = digest(original["task"])
            if len(matching) != 1 or key in seen:
                raise ValueError("gap replay request is duplicate or outside current frozen gap scope")
            seen.add(key)
            source = _reference(root, original["path"], mode, refs, original["sha256"])
            response = _read(source)
            replay = {**deepcopy(original), "replayed": True, "network_requests": 0}
            requests.append(replay)
            try:
                quality = _apply_response(rows, matching[0], response, {"path": str(source), "sha256": original["sha256"]}, mode=mode)
                replay.update(verified=True, missing_dates=quality["missing_dates"], quality_issues=quality["quality_issues"])
            except (ValueError, KeyError, TypeError) as exc:
                replay.update(verified=False, validation_error=str(exc))
                stop = "archived_source_contract_failed"
                break
        replayed = True
    if online and tasks:
        _permission(root)
        for name in ("config/sse_szse_market_providers.json", "config/sse_szse_universe.json"):
            _reference(root, name, mode, refs)
        client = BaoStockF2Client(timeout_seconds=min(20, max_seconds), max_attempts=1, pause_seconds=0.5)
        try:
            for index, task in enumerate(tasks):
                remaining = max_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    stop = "time_budget_exhausted"
                    break
                client.timeout_seconds = min(20, remaining)
                response = client.query("history_f2", **task["parameters"])
                path = output / "responses" / (str(index + 1).zfill(2) + ".json")
                signature = _write_new(path, response)
                reference = {"path": str(path), "sha256": signature}
                refs[str(path)] = reference
                attempts = len(response.get("attempts", [])) or 1
                metrics["requests"] += attempts
                metrics["network_requests"] += attempts
                metrics["retries"] += max(0, attempts - 1)
                metrics["source_rows"] += len(response.get("rows", []))
                metrics["response_bytes"] += _io(path).stat().st_size
                record = {"task": task, **reference, "status": response.get("status"), "error_code": response.get("error_code"),
                    "login": response.get("login"), "fetched_at": response.get("fetched_at"), "row_count": len(response.get("rows", [])),
                    "source_raw_hash": response.get("raw_hash"), "elapsed_seconds": response.get("elapsed_seconds"), "verified": False}
                requests.append(record)
                try:
                    quality = _apply_response(rows, task, response, reference, mode=mode)
                    record.update(verified=True, missing_dates=quality["missing_dates"], quality_issues=quality["quality_issues"])
                except (ValueError, KeyError, TypeError) as exc:
                    record["validation_error"] = str(exc)
                    stop = response.get("status") if not response.get("ok") else "source_contract_failed"
                    break
        finally:
            client.close()
    definitive = {member["security_id"]: sorted(row["date"] for row in rows if row["security_id"] == member["security_id"] and row["definitive_non_trading"])
        for member in selection["members"]}
    for security in securities:
        sid = security["identity"]["security_id"]
        security["history_window_accounted_for"] = set(security["missing_dates"]) <= set(definitive[sid])
    metrics["elapsed_seconds"] = round(time.monotonic() - started, 6)
    result = {"schema_version": SCHEMA, "mode": mode, "purpose": "engineering_validation", "production_eligible": False,
        "selection_id": selection["selection_id"], "selection_content_hash": selection["content_hash"], "target_date": selection["target_date"],
        "source_cutoff_at": selection["cutoff_at"], "observed_at": observed, "completed_at": _now(), "historical_reconstruction": True,
        "revision_run_id": revision, "status": "diagnosed" if rows and all(r["definitive_non_trading"] for r in rows) else "partial" if online or replayed else "offline_diagnosis",
        "source_revision_replayed": str(source_revision) if replayed else None, "new_online_observation": online,
        "rows": rows, "securities": securities, "rule_windows": requirements, "requests": requests, "metrics": metrics,
        "selection_denominator": len(selection["members"]), "diagnosed_security_count": len(securities), "gap_date_count": len(rows),
        "definitive_non_trading_dates_by_security": definitive, "stop_reason": stop, "file_refs": list(refs.values()),
        "model_calls": 0, "model_tokens": 0, "database_writes": 0, "source_endpoint": ENDPOINT,
        "response_byte_measurement": "archived_SDK_JSON_bytes_not_wire_payload_bytes",
        "limitation": "status evidence explains dates but does not add prices, mix source adjustment windows, or relax original strategy inputs",
        "json_path": str(output / "history_gap_diagnosis.json"), "csv_path": str(output / "history_gap_diagnosis.csv")}
    _write_new(output / "history_gap_diagnosis.json", _seal(result))
    stream = io.StringIO(newline="")
    columns = ["security_id", "code", "date", "target_date", "classification", "reason", "definitive_non_trading",
        "raw_record_present", "sina_decoded_record_present", "adjusted_record_present", "factor_present", "within_rule_windows", "historical_state"]
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: json.dumps(row[k], ensure_ascii=False) if isinstance(row[k], (dict, list)) else row[k] for k in columns})
    with _io(output / "history_gap_diagnosis.csv").open("xb") as handle:
        handle.write(stream.getvalue().encode("utf-8-sig"))
    return _seal(result)


def read_gap_diagnosis(root, path, selection, config):
    """Read and independently rederive an immutable diagnosis; never write/network."""
    root = Path(root).resolve()
    mode = selection.get("mode")
    directory, rows, securities, tasks, requirements, expected_refs = _ledger(root, selection, config)
    path = _archive_path(root, path, mode)
    if path.name != "history_gap_diagnosis.json" or path.parent.parent != directory / "f4s1/gap_diagnosis":
        raise ValueError("gap diagnosis path is outside the same isolated selection")
    packet = _verified_file(path)
    required = {"schema_version": SCHEMA, "mode": mode, "purpose": "engineering_validation", "production_eligible": False,
        "selection_id": selection["selection_id"], "selection_content_hash": selection["content_hash"],
        "target_date": selection["target_date"], "source_cutoff_at": selection["cutoff_at"],
        "revision_run_id": path.parent.name, "selection_denominator": len(selection["members"]),
        "diagnosed_security_count": len(securities), "gap_date_count": len(rows), "model_calls": 0, "model_tokens": 0,
        "database_writes": 0, "source_endpoint": ENDPOINT, "historical_reconstruction": True}
    if any(packet.get(key) != value for key, value in required.items()):
        raise ValueError("gap diagnosis selection/purpose/header mismatch")
    now = datetime.now(SHANGHAI)
    observed, completed = (datetime.fromisoformat(packet[key]) for key in ("observed_at", "completed_at"))
    if observed.utcoffset() is None or completed.utcoffset() is None or not observed <= completed <= now:
        raise ValueError("gap diagnosis observation timestamps invalid")
    refs = {}
    for ref in packet.get("file_refs", []):
        _reference(root, ref["path"], mode, refs, ref["sha256"])
    if any(key not in refs or refs[key]["sha256"] != ref["sha256"] for key, ref in expected_refs.items()):
        raise ValueError("gap diagnosis omits a frozen history source reference")
    manifest_path = path.parent / "manifest.json"
    manifest = _verified_file(manifest_path)
    signature = hashlib.sha256(_io(manifest_path).read_bytes()).hexdigest()
    if refs.get(str(manifest_path), {}).get("sha256") != signature:
        raise ValueError("gap manifest is not a frozen source reference")
    if (any(manifest.get(key) != required[key] for key in ("schema_version", "mode", "purpose", "production_eligible",
            "selection_id", "selection_content_hash", "target_date", "source_cutoff_at", "revision_run_id"))
            or manifest.get("tasks") != tasks or manifest.get("max_requests") != 4 or manifest.get("max_attempts") != 1):
        raise ValueError("gap manifest request boundaries differ from frozen history")
    if mode == "research" and packet.get("requests"):
        _permission(root)
    seen, failed = set(), False
    for request in packet.get("requests", []):
        key = digest(request["task"])
        if failed or key in seen or request["task"] not in tasks or len(seen) >= 4:
            raise ValueError("gap request is duplicated, outside scope, or follows a stopped source")
        seen.add(key)
        source = _reference(root, request["path"], mode, {}, request["sha256"])
        if refs.get(str(source), {}).get("sha256") != request["sha256"]:
            raise ValueError("gap SDK source is omitted from file references")
        response = _read(source)
        if datetime.fromisoformat(response["fetched_at"]) > completed:
            raise ValueError("gap source was observed after the reported completion")
        try:
            _apply_response(rows, request["task"], response, {"path": str(source), "sha256": request["sha256"]}, mode=mode)
        except (ValueError, KeyError, TypeError):
            if request.get("verified") is not False:
                raise ValueError("gap report claims a failed SDK source was verified") from None
            failed = True
    definitive = {member["security_id"]: sorted(row["date"] for row in rows if row["security_id"] == member["security_id"] and row["definitive_non_trading"])
        for member in selection["members"]}
    for security in securities:
        security["history_window_accounted_for"] = set(security["missing_dates"]) <= set(definitive[security["identity"]["security_id"]])
    def comparable(value, key=None):
        if isinstance(value, dict):
            return {name: comparable(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [comparable(item) for item in value]
        if key in {"path", "source_path", "source_response_path"} and isinstance(value, str):
            return str(_archive_path(root, value, mode))
        return value
    derived_status = ("diagnosed" if rows and all(row["definitive_non_trading"] for row in rows) else "partial"
        if packet.get("new_online_observation") or packet.get("source_revision_replayed") else "offline_diagnosis")
    for key, derived in (("rows", rows), ("securities", securities), ("rule_windows", requirements),
            ("definitive_non_trading_dates_by_security", definitive), ("status", derived_status)):
        if comparable(packet.get(key)) != comparable(derived):
            raise ValueError("gap diagnosis conclusion differs from original source replay: " + key)
    if any(packet.get("metrics", {}).get(key) != 0 for key in ("price_rows_inserted", "other_selected_security_history_requests", "outside_selection_requests")):
        raise ValueError("gap diagnosis exceeds the no-price-write/selected-only boundary")
    return {**packet, "verified": True, "read_verification": "frozen_history_and_original_sdk_replayed_no_network_or_writes"}
