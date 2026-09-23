"""Offline, hash-bound status evidence overlays for dated sector observations.

This module has no network or database writer. It never edits U or converts an
unknown research risk into a normal status. A late observation remains a late
historical reconstruction even when its source row describes an earlier day.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path

from ..calendar import _digest, _verified_rows
from ..market_foundation import _hash, normalize_baostock_rows
from ..operations.backup import _io
from ..operations.paths import resolve_archived_path
from .baostock import BaoStockClient, SHANGHAI, validate_request

SCHEMA = "f2s1-zero-quote-status-v1"
ENDPOINT = "baostock://public-api.baostock.com:10030/query_history_k_data_plus"


def _strict_json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("status_duplicate_json_key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("status_nonfinite_json_value")
    return json.loads(body.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=invalid)


def _read(root, locator, refs=None, expected=None):
    if not isinstance(locator, (str, Path)) or not str(locator):
        raise ValueError("status_source_locator_missing")
    candidate = Path(locator)
    if not candidate.is_absolute():
        candidate = root / candidate
    path = resolve_archived_path(str(candidate), anchor=root).resolve()
    if not path.is_relative_to(root):
        raise ValueError("status_source_path_outside_project")
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if _io(parent).is_symlink() or (hasattr(parent, "is_junction") and _io(parent).is_junction()):
            raise ValueError("status_source_link_not_allowed")
    if _io(path).stat().st_size > 8_000_000:
        raise ValueError("status_source_file_too_large")
    body = _io(path).read_bytes()
    actual = hashlib.sha256(body).hexdigest()
    if expected is not None and actual != expected:
        raise ValueError("status_source_file_hash_mismatch")
    if refs is not None:
        if str(path) in refs and refs[str(path)]["sha256"] != actual:
            raise ValueError("status_source_reference_conflict")
        refs[str(path)] = {"path": str(path), "sha256": actual}
    return _strict_json(body), path, actual


def _stamp(value, target):
    observed = datetime.fromisoformat(value)
    if observed.utcoffset() is None or observed > datetime.now(SHANGHAI) or observed.astimezone(SHANGHAI).date() < date.fromisoformat(target):
        raise ValueError("status_source_observation_date_invalid")
    return observed


def _permission(root):
    config, _, _ = _read(root, "config/sse_szse_market_providers.json")
    if (config.get("schema_version") != "f2-market-config-v1" or config.get("scope") != "sse_szse_a"
            or config.get("provider") != "baostock" or config.get("permission_status") != "approved"
            or not config.get("permission_basis") or config.get("model_calls") != 0):
        raise ValueError("status_source_permission_required")
    universe_config, _, _ = _read(root, config.get("universe_config", "config/sse_szse_universe.json"))
    sources = [s for s in universe_config.get("sources", []) if s.get("provider") == "baostock"]
    if len(sources) != 1 or any(sources[0].get(k) != v for k, v in
            (("enabled", True), ("permission_status", "approved"), ("llm_export", False))) or not sources[0].get("permission_basis"):
        raise ValueError("status_source_permission_required")


def _response(root, ref, symbol, identity, target, mode, refs, *, cached):
    locator = ref.get("source_path") if cached else ref.get("path")
    expected = ref.get("source_file_hash")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("status_source_expected_hash_missing")
    response, path, source_hash = _read(root, locator, refs, expected)
    params = validate_request("history_f2", response.get("parameters", {}))
    if (params["code"] != symbol or params["security_type"] != "stock" or params["adjustment_mode"] != "unadjusted"
            or not params["start_date"] <= target <= params["end_date"]
            or (not cached and (params["start_date"] != target or params["end_date"] != target))):
        raise ValueError("status_response_request_identity_or_date_mismatch")
    if not BaoStockClient._valid_worker_result(response, "history_f2", params) or response.get("ok") is not True:
        raise ValueError("status_response_sdk_contract_invalid")
    expected_mode = "online" if mode == "research" else "offline_test"
    expected_kind = "live_network" if mode == "research" else "offline_test"
    if any(k in response and response[k] != v for k, v in
            (("mode", mode), ("provenance_mode", expected_mode), ("verification_kind", expected_kind))):
        raise ValueError("status_response_test_provenance_conflict")
    if mode == "offline_test" and (response.get("provenance_mode") != "offline_test" or response.get("verification_kind") != "offline_test"):
        raise ValueError("status_offline_response_provenance_missing")
    if response["raw_hash"] != ref.get("source_raw_hash" if cached else "raw_hash"):
        raise ValueError("status_source_raw_hash_mismatch")
    observed = _stamp(response["fetched_at"], target)
    rows = response["rows"]
    if (len({r["date"] for r in rows}) != len(rows)
            or any(r["code"] != symbol or not params["start_date"] <= r["date"] <= params["end_date"] for r in rows)):
        raise ValueError("status_response_rows_identity_date_conflict")
    for row in rows:
        if date.fromisoformat(row["date"]).isoformat() != row["date"] or row["adjustflag"] != "3":
            raise ValueError("status_response_row_date_invalid")
    diagnostics = response.get("diagnostics", {})
    events = diagnostics.get("events", [])
    query = [e for e in events if e.get("stage") == "query_wait" and e.get("state") == "completed"]
    ended = [e for e in events if e.get("stage") == "row_read" and e.get("state") == "completed"]
    # The existing bounded SDK reader reports completion only after next() is
    # false and its error code has been checked. The complete initial page is
    # independently counted; no invented pagination fields are required.
    if (diagnostics.get("failure_stage") is not None or diagnostics.get("hard_timeout") is not False
            or len(query) != 1 or query[0].get("error_code") != "0"
            or query[0].get("initial_page") not in (1, "1") or query[0].get("initial_page_records") != len(rows)
            or len(ended) != 1 or ended[0].get("row_count") != len(rows)):
        raise ValueError("status_response_reader_end_unverified")
    targets = [row for row in rows if row["date"] == target]
    if len(targets) != 1 or targets[0]["tradestatus"] not in {"0", "1"}:
        raise ValueError("status_target_row_missing_duplicate_or_unknown")
    if cached:
        quality = normalize_baostock_rows(targets, security_id=identity["security_id"], symbol=symbol,
            start_date=target, end_date=target, trading_dates=(target,))
        if len(quality["records"]) != 1 or _hash(quality["records"][0]) != ref.get("fact_hash"):
            raise ValueError("status_cached_fact_hash_mismatch")
    value = targets[0]["tradestatus"] == "0"
    return value, {"value": value, "verified": True, "source": "baostock", "full_day": value,
        "as_of_date": target, "effective_from": target, "effective_to": target,
        "source_path": str(path), "source_file_hash": source_hash, "source_raw_hash": response["raw_hash"],
        "observed_at": response["fetched_at"], "historical_reconstruction": observed.astimezone(SHANGHAI).date().isoformat() > target,
        "evidence_id": "baostock-status-" + _digest({"security_id": identity["security_id"], "target": target, "source_hash": source_hash})}


def apply_status_evidence(root, universe, quotes, target, evidence_path):
    """Return a new quote packet after revalidating actual dated SDK evidence."""
    root = Path(root).resolve()
    if not isinstance(target, str) or date.fromisoformat(target).isoformat() != target:
        raise ValueError("status_target_date_invalid")
    mode = universe.get("mode")
    if mode not in {"research", "offline_test"}:
        raise ValueError("status_universe_mode_invalid")
    if (universe.get("scope") != "sse_szse_a" or universe.get("requested_date") != target
            or universe.get("resolved_trade_date") != target or universe.get("universe_verified") is not True
            or universe.get("calendar_verified") is not True or universe.get("collection_ready") is not True):
        raise ValueError("status_universe_date_or_verification_invalid")
    _permission(root)
    refs = {}
    for ref in quotes.get("file_refs", []):
        _read(root, ref["path"], refs, ref["sha256"])
    result, result_path, result_hash = _read(root, evidence_path, refs)
    manifest, _, _ = _read(root, result_path.parent / "manifest.json", refs)
    if (result.get("schema_version") != SCHEMA or manifest.get("schema_version") != SCHEMA
            or result.get("target_date") != target or manifest.get("target_date") != target
            or manifest.get("mode") != mode or manifest.get("scope") != "sse_szse_a"
            or manifest.get("universe_snapshot_id") != universe.get("snapshot_id")
            or manifest.get("source_endpoint") != ENDPOINT or not manifest.get("permission_basis")
            or manifest.get("online_requested") is not (mode == "research")):
        raise ValueError("status_manifest_scope_date_identity_or_provenance_invalid")
    if mode == "research" and manifest.get("environment_label") not in {"approved_require_escalated_windows_local", "daily_observation_live"}:
        raise ValueError("status_manifest_live_environment_unverified")
    if mode == "offline_test" and "research" in {part.casefold() for part in result_path.parts}:
        raise ValueError("status_offline_evidence_in_research_path")
    _stamp(manifest["observed_at"], target)
    expected_provenance = "online" if mode == "research" else "offline_test"
    if quotes.get("provenance_mode") != expected_provenance:
        raise ValueError("status_quote_provenance_mismatch")
    cal = manifest.get("calendar", {})
    if not isinstance(cal.get("source_file_hash"), str) or len(cal["source_file_hash"]) != 64:
        raise ValueError("status_calendar_expected_hash_missing")
    packet, _, _ = _read(root, cal.get("source_path"), refs, cal.get("source_file_hash"))
    if (packet.get("schema_version") != "f1-calendar-cache-v1" or packet.get("mode") != mode
            or packet.get("provider") != "baostock" or packet.get("content_hash") != _digest({k: v for k, v in packet.items() if k != "content_hash"})
            or packet.get("response", {}).get("raw_hash") != cal.get("source_raw_hash")
            or _verified_rows(packet["response"]).get(date.fromisoformat(target)) is not True):
        raise ValueError("status_calendar_evidence_invalid")
    members, rows = {}, {}
    for member in universe.get("members", []):
        symbol = {"SSE": "sh.", "SZSE": "sz."}.get(member.get("exchange"), "?") + member.get("code", "")
        if symbol in members:
            raise ValueError("status_universe_duplicate_identity")
        members[symbol] = member
    new_quotes = deepcopy(quotes)
    for row in new_quotes.get("rows", []):
        symbol = row.get("symbol")
        if symbol in rows:
            raise ValueError("status_quote_duplicate_identity")
        rows[symbol] = row
    identities = manifest.get("identities", {})
    if not identities or not isinstance(identities, dict):
        raise ValueError("status_manifest_identity_missing")
    cached, online = result.get("cached", {}), result.get("online", {})
    if set(cached) - set(identities) or set(online) - set(identities):
        raise ValueError("status_unrequested_evidence_identity")
    applied = []
    for symbol, identity in identities.items():
        member = members.get(symbol)
        if (member is None or any(identity.get(k) != member.get(k) for k in
                ("security_id", "code", "exchange", "board", "security_type", "metadata_verified"))
                or member.get("security_type") != "ordinary_a" or member.get("metadata_verified") is not True
                or member.get("metadata_conflict") or (mode == "research" and member.get("provenance_mode") == "offline_test")):
            raise ValueError("status_security_identity_conflict")
        quote = rows.get(symbol.replace(".", ""))
        if (quote is None or quote.get("security_id") != identity["security_id"] or quote.get("trade_date") != target
                or any(quote.get("identity", {}).get(key) != identity[key] for key in
                       ("security_id", "code", "exchange", "board", "security_type", "metadata_verified"))):
            raise ValueError("status_quote_identity_or_date_conflict")
        observations = [_response(root, ref, symbol, identity, target, mode, refs, cached=True)
                        for ref in cached.get(symbol, {}).get("evidence", [])]
        if symbol in online:
            observations.append(_response(root, online[symbol], symbol, identity, target, mode, refs, cached=False))
        if not observations:
            continue
        if len({value for value, _ in observations}) != 1:
            raise ValueError("status_sources_conflict")
        halted, evidence = max(observations, key=lambda item: item[1]["observed_at"])
        if not halted:
            if quote.get("full_day_halt_evidence"):
                raise ValueError("status_existing_overlay_conflict")
            continue  # A trading source row does not resolve other unknown risks.
        if quote.get("tradestatus") is True:
            raise ValueError("status_quote_trading_conflict")
        previous = quote.get("full_day_halt_evidence")
        if previous is not None and previous != evidence:
            raise ValueError("status_existing_overlay_conflict")
        quote["full_day_halt_evidence"] = evidence
        applied.append(identity["security_id"])
    new_quotes["file_refs"] = [refs[key] for key in sorted(refs)]
    overlay = {"schema_version": SCHEMA, "target_date": target, "result_path": str(result_path),
        "result_sha256": result_hash, "applied_security_ids": sorted(applied), "network_requests": 0,
        "kind": "dated_status_overlay_original_universe_and_quote_facts_preserved"}
    existing = new_quotes.setdefault("status_evidence_overlays", [])
    if overlay not in existing:
        existing.append(overlay)
    return new_quotes
