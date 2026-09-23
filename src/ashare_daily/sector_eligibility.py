"""Field-level dated qualification, reusing the existing eligibility resolver.

Source imports are replayed from immutable bytes before their conclusions are
accepted. This module never writes a market database or calls a research model.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .eligibility import load_evidence_bundle, resolve_eligibility
from .operations.backup import _io
from .operations.paths import resolve_archived_path
from .providers.baostock import BaoStockClient, SHANGHAI, validate_request
from .qualification_sources import SSE_URL, SZSE_URL, parse_sse_list, parse_szse_page
from .screening.m21 import ELIGIBILITY_IDS, _combined
from .sector_selection import digest, verify_selection

PACKET_SCHEMA = "f4s1-field-facts-v1"
RESULT_SCHEMA = "f4s1-eligibility-result-v1"
LEGACY_BUNDLE = "outputs/verification/enhancement/live-eligibility/20260911T111258109238-3f2230ce/eligibility.json"
RISK_FIELDS = ("st", "suspended", "delisting_period")
FIELD_CONDITIONS = {"identity": "identity", "listed": "listed", "st": "not_st",
    "suspended": "not_suspended", "delisting_period": "not_delisting_period"}


class _VerifiedFieldFacts(dict):
    """Only read_field_facts constructs the source-replayed import object."""


def _stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("qualification evidence time requires timezone")
    return stamp.astimezone(SHANGHAI)


def _symbol(member):
    return {"SSE": "sh.", "SZSE": "sz."}[member["exchange"]] + member["code"]


def _read(root, locator, expected=None):
    path = Path(locator)
    if not path.is_absolute():
        path = root / path
    path = resolve_archived_path(str(path), anchor=root).resolve()
    if not path.is_relative_to(root):
        raise ValueError("qualification source path outside project")
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if _io(parent).is_symlink() or (hasattr(parent, "is_junction") and _io(parent).is_junction()):
            raise ValueError("qualification source path link rejected")
    if _io(path).stat().st_size > 10_000_000:
        raise ValueError("qualification source exceeds bounded file size")
    raw = _io(path).read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    if expected is not None and checksum != expected:
        raise ValueError("qualification source file hash mismatch")
    return raw, {"path": str(path), "sha256": checksum}


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("qualification source duplicate JSON key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("qualification source nonfinite JSON")
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=invalid)


def _write(path, value):
    _io(path.parent).mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
    with _io(path).open("xb") as stream:
        stream.write(raw)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}


def _fact(member, field, value, target, *, source_id=None, evidence_id=None,
          effective_from=None, effective_to=None, source_business_date=None,
          observed_at=None, fetched_at=None, published_at=None, completeness_scope=None,
          reason, verified=False, conflict=False, proof=None, mode="research"):
    fact = {"security_id": member["security_id"], "symbol": _symbol(member), "field": field,
        "value": value, "target_date": target, "effective_from": effective_from, "effective_to": effective_to,
        "source_id": source_id, "evidence_id": evidence_id, "source_business_date": source_business_date,
        "published_at": published_at, "observed_at": observed_at, "fetched_at": fetched_at,
        "completeness_scope": deepcopy(completeness_scope), "conflict": conflict, "reason": reason,
        "verified": verified, "mode": mode, "provenance_mode": "online" if mode == "research" else "offline_test",
        "historical_reconstruction": bool(fetched_at and fetched_at[:10] > target), "proof": deepcopy(proof)}
    fact["fact_hash"] = digest(fact)
    return fact


def _source_permission(root):
    raw, ref = _read(root, "config/sse_szse_market_providers.json")
    config = _json(raw)
    if (config.get("provider") != "baostock" or config.get("permission_status") != "approved"
            or not config.get("permission_basis") or config.get("model_calls") != 0):
        raise ValueError("existing BaoStock local research permission unavailable")
    return ref


def _bao_facts(member, response, ref, target, cutoff, mode):
    operation = response.get("operation")
    if operation not in {"history", "history_f2"}:
        raise ValueError("qualification requires an existing daily history operation")
    params = validate_request(operation, response.get("parameters", {}))
    if (params["code"] != _symbol(member) or params["security_type"] != "stock"
            or params["adjustment_mode"] != "unadjusted" or not params["start_date"] <= target <= params["end_date"]
            or not BaoStockClient._valid_worker_result(response, operation, params) or response.get("ok") is not True):
        raise ValueError("BaoStock qualification request/identity/response contract failed")
    observed = _stamp(response["fetched_at"])
    if observed > cutoff or observed.date().isoformat() < target:
        raise ValueError("BaoStock qualification observation date invalid")
    expected_mode = "online" if mode == "research" else "offline_test"
    expected_kind = "live_network" if mode == "research" else "offline_test"
    if any(key in response and response[key] != expected for key, expected in
           (("mode", mode), ("provenance_mode", expected_mode), ("verification_kind", expected_kind))):
        raise ValueError("BaoStock qualification provenance conflict")
    if mode == "offline_test" and response.get("verification_kind") != "offline_test":
        raise ValueError("test response lacks explicit offline provenance")
    rows = response["rows"]
    if (len({row["date"] for row in rows}) != len(rows)
            or any(row["code"] != params["code"] or not params["start_date"] <= row["date"] <= params["end_date"]
                   or row["adjustflag"] != "3" for row in rows)):
        raise ValueError("BaoStock state row identity/date/adjustment conflict")
    targets = [row for row in rows if row["date"] == target]
    if len(targets) != 1:
        raise ValueError("BaoStock target state row missing or duplicated")
    diagnostic = response.get("diagnostics")
    if diagnostic is not None:
        completed = [event for event in diagnostic.get("events", []) if event.get("stage") == "row_read" and event.get("state") == "completed"]
        if (diagnostic.get("failure_stage") is not None or diagnostic.get("hard_timeout") is not False
                or len(completed) != 1 or completed[0].get("row_count") != len(rows)):
            raise ValueError("BaoStock bounded reader end is unverified")
    row = targets[0]
    facts = []
    for field, source_field in (("st", "isST"), ("suspended", "tradestatus")):
        token = row[source_field]
        value = (token == "1" if field == "st" else token == "0") if token in {"0", "1"} else None
        facts.append(_fact(member, field, value, target, source_id="baostock_dated_daily_state",
            evidence_id="baostock-state-" + digest({"response": ref["sha256"], "target": target, "field": field}),
            effective_from=target, effective_to=target, source_business_date=target,
            observed_at=response["fetched_at"], fetched_at=response["fetched_at"],
            completeness_scope={"kind": "individual_dated_state", "security_ids": [member["security_id"]],
                "boards": [member["board"]], "field": field, "complete": True, "source_field": source_field},
            reason="BaoStock目标日原始状态字段直接证据" if value is not None else "BaoStock目标日状态字段未知；不按正常处理",
            verified=value is not None, proof={"file_ref": ref, "raw_hash": response["raw_hash"], "parameters": params,
                "raw_value": token, "source_field": source_field}, mode=mode))
    return facts


def _legacy_facts(root, selection, proof, cutoff, refs):
    raw, actual_bundle_ref = _read(root, proof["bundle_ref"]["path"], proof["bundle_ref"]["sha256"])
    bundle_ref = deepcopy(proof["bundle_ref"])
    payload = _json(raw)
    bundle = load_evidence_bundle(_io(Path(actual_bundle_ref["path"])))
    refs[bundle_ref["path"]] = bundle_ref
    health_raw, _ = _read(root, proof["health_ref"]["path"], proof["health_ref"]["sha256"])
    health_ref = deepcopy(proof["health_ref"])
    health = _json(health_raw)
    refs[health_ref["path"]] = health_ref
    mode, target = selection["mode"], selection["target_date"]
    kind = "automatic_source" if mode == "research" else "offline_test"
    if payload["verification_kind"] != kind or health.get("verification_kind") != kind:
        raise ValueError("legacy qualification provenance conflict")
    sources = {source["source_id"]: source for source in payload["sources"]}
    health_by_source = {item["source_id"]: item for item in health["source_health"]}
    source_dates = {}
    for record in payload["records"]:
        sid = record["source_id"]
        if sid not in {"sse_mainboard_delisting", "szse_delisting"}:
            raise ValueError("legacy qualification source is not an existing reviewed adapter")
        expected_url = SSE_URL if sid == "sse_mainboard_delisting" else SZSE_URL
        source, state = sources[sid], health_by_source[sid]
        if (source["source_url"] != expected_url or record["source_url"] != expected_url
                or source.get("approved_for_local_use") is not True or state.get("status") != "ok"
                or state.get("target_date") != target or _stamp(record["fetched_at"]) > cutoff):
            raise ValueError("legacy qualification source permission/status/date mismatch")
        parsed_members = []
        totals, page_counts = set(), set()
        for page in record["pages"]:
            page_raw, page_ref = _read(root, page["raw_locator"], page["raw_sha256"])
            # Restores relocate reads through the verified archive map, while
            # original source locators remain part of the immutable fact hash.
            page_ref = {"path": page["raw_locator"], "sha256": page_ref["sha256"]}
            refs[page_ref["path"]] = page_ref
            requests = [request for request in state.get("requests", [])
                if request.get("role") == "page" + str(page["number"])]
            if (len(requests) != 1 or requests[0].get("http_status") != 200
                    or requests[0].get("url") != page["source_url"] or requests[0].get("raw_sha256") != page_ref["sha256"]
                    or _stamp(requests[0]["fetched_at"]) > cutoff):
                raise ValueError("legacy raw page does not match real HTTP evidence")
            decoded = _json(page_raw)
            if sid == "sse_mainboard_delisting":
                parsed = parse_sse_list(page_raw)
                total, page_count = decoded["pageHelp"]["total"], 1
                source_dates[sid] = decoded.get("queryDate") or None
                if page["source_url"] != SSE_URL:
                    raise ValueError("SSE qualification page request differs")
            else:
                parsed_page = parse_szse_page(page_raw, date.fromisoformat(target), page["number"])
                parsed, total, page_count = parsed_page["symbols"], parsed_page["total"], parsed_page["pages"]
                tab = next(item for item in decoded if item["metadata"]["tabkey"] == "tab2")
                source_dates[sid] = tab["metadata"].get("subname", "").strip() or None
                expected = SZSE_URL if page["number"] == 1 else SZSE_URL + "&TABKEY=tab2&PAGENO=" + str(page["number"])
                if page["source_url"] != expected:
                    raise ValueError("SZSE qualification page request differs")
            if parsed != page["symbols"]:
                raise ValueError("legacy parsed members differ from original source bytes")
            parsed_members.extend(parsed)
            totals.add(total)
            page_counts.add(page_count)
        if (totals != {record["expected_total_records"]} or page_counts != {record["total_pages"]}
                or len(parsed_members) != record["expected_total_records"] or len(set(parsed_members)) != len(parsed_members)):
            raise ValueError("legacy complete-list boundary mismatch")
        if source_dates[sid] is not None and source_dates[sid] != target:
            raise ValueError("legacy source business date differs from target")
    resolved = resolve_eligibility(bundle, [_symbol(member) for member in selection["members"]], target)
    facts = []
    for member in selection["members"]:
        state = resolved[_symbol(member)]
        applicable = member.get("board") in {"sse_main", "szse_main"}
        value = state["delisting_period"] if applicable else None
        evidence = state["evidence"]
        source_ids = sorted({item["source"]["source_id"] for item in evidence})
        records = [item["record"] for item in evidence]
        source_date_values = {source_dates[sid] for sid in source_ids}
        facts.append(_fact(member, "delisting_period", value, target,
            source_id="+".join(source_ids) or "legacy_delisting_resolver", evidence_id=state["evidence_id"],
            effective_from=target if value is not None else None, effective_to=target if value is not None else None,
            source_business_date=next(iter(source_date_values)) if len(source_date_values) == 1 else None,
            observed_at=min((item["first_seen_at"] for item in records), default=None),
            fetched_at=max((item["fetched_at"] for item in records), default=None),
            completeness_scope={"kind": "same_date_complete_snapshot", "boards": ["sse_main" if member["exchange"] == "SSE" else "szse_main"],
                "field": "delisting_period", "complete": value is not None, "source_date_basis": "observed_on_target_date",
                "does_not_claim_source_business_date": True},
            reason=state["reason"] if applicable else "旧退市名单仅核验主板覆盖；不得推广到创业板或科创板",
            verified=value is not None, conflict=len({item["value"] for item in evidence}) > 1,
            proof={"bundle_ref": bundle_ref, "health_ref": health_ref, "resolver_state": state}, mode=mode))
    return facts


def _replay(root, selection, proofs, cutoff):
    members = {member["security_id"]: member for member in selection["members"]}
    facts, refs = [], {}
    for proof in proofs:
        if proof.get("kind") == "baostock_response":
            member = members.get(proof.get("security_id"))
            if member is None:
                raise ValueError("qualification proof names an out-of-selection security")
            raw, ref = _read(root, proof["file_ref"]["path"], proof["file_ref"]["sha256"])
            ref = deepcopy(proof["file_ref"])
            refs[ref["path"]] = ref
            facts += _bao_facts(member, _json(raw), ref, selection["target_date"], cutoff, selection["mode"])
        elif proof.get("kind") == "legacy_delisting_bundle":
            facts += _legacy_facts(root, selection, proof, cutoff, refs)
        else:
            raise ValueError("qualification proof kind not implemented")
    facts.sort(key=lambda item: (item["security_id"], item["field"], item["fact_hash"]))
    return facts, [refs[key] for key in sorted(refs)]


def read_field_facts(root, path, selection):
    """Recheck source bytes, source contracts and derived values, not just IDs."""
    root = Path(root).resolve()
    verify_selection(selection)
    raw, ref = _read(root, path)
    packet = _json(raw)
    if (packet.get("schema_version") != PACKET_SCHEMA or packet.get("content_hash") != digest({key: value for key, value in packet.items() if key != "content_hash"})
            or packet.get("selection_id") != selection["selection_id"] or packet.get("selection_content_hash") != selection["content_hash"]
            or packet.get("target_date") != selection["target_date"] or packet.get("mode") != selection["mode"]
            or packet.get("purpose") != selection.get("purpose", "production")
            or packet.get("production_eligible") is not (selection.get("purpose", "production") == "production")):
        raise ValueError("qualification packet selection/provenance/hash mismatch")
    cutoff = _stamp(packet["cutoff_at"])
    if cutoff > datetime.now(SHANGHAI):
        raise ValueError("qualification packet cutoff in future")
    rebuilt, refs = _replay(root, selection, packet["proofs"], cutoff)
    if packet.get("facts") != rebuilt or packet.get("file_refs") != refs:
        raise ValueError("qualification facts differ from source replay")
    result = _VerifiedFieldFacts(packet)
    result.file_ref = ref
    result._verified_hash = packet["content_hash"]
    return result


def _cached_bao_proofs(root, selection, cutoff):
    proofs = []
    databases = ["data/research/market.sqlite3"]
    if selection.get("purpose") == "engineering_validation":
        databases.append("data/engineering_validation/f3s/market.sqlite3")
    for database in databases:
        path = root / database
        if not _io(path).is_file():
            continue
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"f2_schema", "f2_batches", "f2_bar_observations"}
            if not tables.intersection(required):
                # A managed legacy cache can legitimately contain no history
                # yet. It supplies no dated qualification proofs.
                continue
            if not required <= tables or connection.execute("SELECT version,mode FROM f2_schema").fetchall() != [(1, selection["mode"])]:
                raise ValueError("qualification_history_cache_schema_mismatch")
            for member in selection["members"]:
                records = connection.execute("SELECT DISTINCT b.source_response_path,b.source_file_hash,b.provenance_json "
                    "FROM f2_batches b JOIN f2_bar_observations o USING(batch_id) "
                    "WHERE b.security_id=? AND b.adjustment_mode='unadjusted' AND o.trade_date=?",
                    (member["security_id"], selection["target_date"])).fetchall()
                for locator, signature, provenance in records:
                    info = json.loads(provenance)
                    if info.get("provider") != "baostock":
                        continue
                    try:
                        raw, ref = _read(root, locator, signature)
                        _bao_facts(member, _json(raw), ref, selection["target_date"], cutoff, selection["mode"])
                    except (ValueError, OSError):
                        continue
                    proof = {"kind": "baostock_response", "security_id": member["security_id"], "file_ref": ref, "origin": "existing_archive"}
                    if proof not in proofs:
                        proofs.append(proof)
    return proofs


def collect_sector_eligibility(root, selection, *, output_directory, online=False, cutoff_at=None, mode=None, transport=None, max_seconds=120,
                               legacy_bundle=None, max_probe_members=7, probe_security_ids=None):
    """Append one bounded field-evidence packet; existing original data is read-only."""
    root = Path(root).resolve()
    if (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
            or not math.isfinite(max_seconds) or not 0 < max_seconds <= 14400):
        raise ValueError("qualification runtime limit must be positive and at most 14400 seconds")
    deadline = time.monotonic() + max_seconds
    verify_selection(selection)
    if type(max_probe_members) is not int or not 0 < max_probe_members <= 10000:
        raise ValueError("qualification maximum probe members must be a bounded positive integer")
    selected_ids = {member["security_id"] for member in selection["members"]}
    probe_ids = set(probe_security_ids) if probe_security_ids is not None else selected_ids
    probe_order = {sid: index for index, sid in enumerate(probe_security_ids or [])}
    if not probe_ids <= selected_ids:
        raise ValueError("qualification probe names an out-of-selection security")
    if online and len(probe_ids) > max_probe_members:
        raise ValueError("this explicit field probe is bounded to seven frozen engineering members" if max_probe_members == 7
                         else "qualification probe exceeds explicit maximum member count")
    mode = mode or selection["mode"]
    if mode != selection["mode"] or mode not in {"research", "offline_test"}:
        raise ValueError("qualification collection mode conflicts with selection")
    if mode == "research" and transport is not None or mode == "offline_test" and online and transport is None:
        raise ValueError("test transport is explicit and cannot enter research")
    directory = Path(output_directory)
    if not directory.is_absolute():
        directory = root / directory
    directory = directory.resolve() / (datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8])
    if not directory.is_relative_to(root):
        raise ValueError("qualification output outside project")
    if mode == "offline_test" and "research" in {part.casefold() for part in directory.parts}:
        raise ValueError("qualification test cannot write research namespace")
    _io(directory).mkdir(parents=True, exist_ok=False)
    started = _stamp(cutoff_at) if cutoff_at else datetime.now(SHANGHAI)
    proofs = _cached_bao_proofs(root, selection, started) if selection["members"] else []
    source_health = []
    legacy = root / (legacy_bundle if legacy_bundle is not None else LEGACY_BUNDLE)
    if selection["members"] and _io(legacy).is_file():
        try:
            _, bundle_ref = _read(root, legacy)
            _, health_ref = _read(root, legacy.parent / "result.json")
            proof = {"kind": "legacy_delisting_bundle", "bundle_ref": bundle_ref, "health_ref": health_ref}
            _replay(root, selection, [proof], started)
            proofs.append(proof)
            source_health.append({"source_id": "existing_exchange_delisting_snapshots", "status": "source_reverified",
                "source_business_date": None, "target_date": selection["target_date"], "network_requests": 0})
        except (ValueError, OSError, KeyError) as exc:
            source_health.append({"source_id": "existing_exchange_delisting_snapshots", "status": "blocked", "reason": str(exc)})
    requests = 0
    if online and selection["members"]:
        _source_permission(root)
        client = BaoStockClient(timeout_seconds=20, max_attempts=1, pause_seconds=.5, diagnostic_stages=True)
        cached_ids = {proof["security_id"] for proof in proofs if proof["kind"] == "baostock_response"}
        for member in sorted(selection["members"], key=lambda item: (probe_order.get(item["security_id"], len(probe_order)), item["security_id"])):
            if member["security_id"] not in probe_ids:
                continue
            if member["security_id"] in cached_ids:
                continue
            if requests:
                remaining = deadline-time.monotonic()
                if remaining > 0:
                    time.sleep(min(.5, remaining))
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                source_health.append({"source_id": "baostock_dated_daily_state", "status": "runtime_limit_exhausted",
                    "security_id": member["security_id"], "request_sent": False, "execution_stopped": True})
                break
            client.timeout_seconds = min(20, remaining)
            parameters = {"code": _symbol(member), "start_date": selection["target_date"], "end_date": selection["target_date"],
                "security_type": "stock", "adjustment_mode": "unadjusted"}
            response = transport("history_f2", **parameters) if transport else client.query("history_f2", **parameters)
            requests += 1
            ref = _write(directory / (member["code"] + "-baostock-response.json"), response)
            observed_cutoff = _stamp(cutoff_at) if cutoff_at else datetime.now(SHANGHAI)
            health = {"source_id": "baostock_dated_daily_state", "security_id": member["security_id"], "symbol": _symbol(member),
                "parameters": parameters, "response_ref": ref, "ok": response.get("ok"), "status": response.get("status"),
                "error_code": response.get("error_code"), "fetched_at": response.get("fetched_at"),
                "failure_stage": response.get("diagnostics", {}).get("failure_stage"), "login": response.get("login"),
                "attempts": len(response.get("attempts", [])), "elapsed_seconds": response.get("elapsed_seconds")}
            source_health.append(health)
            try:
                _bao_facts(member, response, ref, selection["target_date"], observed_cutoff, mode)
            except (ValueError, KeyError, TypeError) as exc:
                health.update(accepted=False, reason=str(exc), source_stopped=True)
                break
            health["accepted"] = True
            proofs.append({"kind": "baostock_response", "security_id": member["security_id"], "file_ref": ref, "origin": "this_probe"})
    cutoff = _stamp(cutoff_at) if cutoff_at else datetime.now(SHANGHAI)
    facts, refs = _replay(root, selection, proofs, cutoff)
    packet = {"schema_version": PACKET_SCHEMA, "mode": mode, "purpose": selection.get("purpose", "production"),
        "production_eligible": selection.get("purpose", "production") == "production", "selection_id": selection["selection_id"],
        "selection_content_hash": selection["content_hash"], "target_date": selection["target_date"], "cutoff_at": cutoff.isoformat(),
        "historical_reconstruction": cutoff.date().isoformat() > selection["target_date"], "facts": facts,
        "proofs": proofs, "file_refs": refs, "source_health": source_health, "query_attempts": requests,
        "runtime_limit_seconds": max_seconds,
        "probe_security_ids": sorted(probe_ids), "max_probe_members": max_probe_members,
        "network_requests": requests if mode == "research" else 0, "database_writes": 0, "model_calls": 0, "model_tokens": 0}
    packet["content_hash"] = digest(packet)
    _write(directory / "field_facts.json", packet)
    return read_field_facts(root, directory / "field_facts.json", selection)


def _inherited_risk_fact(member, row, technical_report, field, cutoff):
    value = row.get("risk_states", {}).get(field)
    evidence = row.get("risk_evidence", {}).get(field, [])
    if type(value) is not bool or not evidence:
        return None
    target = technical_report.get("target_date")
    observed = []
    for item in evidence:
        effective = item.get("effective_date") or item.get("as_of_date")
        locator = item.get("evidence_id") or item.get("evidence_ids") or item.get("fact_hash")
        stamp = item.get("observed_at") or item.get("fetched_at") or item.get("first_seen_at")
        try:
            if effective != target or not locator or _stamp(stamp) > cutoff:
                return None
        except (TypeError, ValueError):
            return None
        observed.append(stamp)
    return _fact(member, field, value, target, source_id="frozen_screening_state_evidence",
        evidence_id="inherited-state-"+digest({"result_hash": technical_report["result_hash"], "security_id": member["security_id"], "field": field}),
        effective_from=target, effective_to=target, observed_at=min(observed), fetched_at=None,
        completeness_scope={"kind": "existing_dated_state_evidence", "security_ids": [member["security_id"]], "field": field},
        reason="原冻结技术报告内已有的目标日状态证据；与新增来源同等参与冲突核验",
        verified=True, proof={"technical_result_hash": technical_report["result_hash"], "evidence": deepcopy(evidence)},
        mode=technical_report["mode"])


def evaluate_sector_eligibility(selection, technical_report, field_facts, *, cutoff_at, require_delisting_check=True):
    """Retain every fact while aggregating only the explicitly required checks."""
    if type(require_delisting_check) is not bool:
        raise ValueError("qualification delisting policy must be boolean")
    verify_selection(selection)
    if not isinstance(field_facts, _VerifiedFieldFacts):
        raise ValueError("qualification imports must be replayed by read_field_facts")
    if (field_facts["content_hash"] != field_facts._verified_hash
            or field_facts["content_hash"] != digest({key: value for key, value in field_facts.items() if key != "content_hash"})):
        raise ValueError("verified qualification packet was modified")
    if (technical_report.get("result_hash") != digest({key: value for key, value in technical_report.items() if key != "result_hash"})
            or technical_report.get("selection_id") != selection["selection_id"] or field_facts["selection_id"] != selection["selection_id"]
            or technical_report.get("purpose") != selection.get("purpose", "production")
            or technical_report.get("mode") != selection["mode"]):
        raise ValueError("qualification technical report selection/hash/provenance mismatch")
    cutoff = _stamp(cutoff_at)
    if cutoff > datetime.now(SHANGHAI) or cutoff < _stamp(field_facts["cutoff_at"]) or cutoff < _stamp(technical_report["cutoff_at"]):
        raise ValueError("qualification actual cutoff invalid")
    reports = {row["security_id"]: row for row in technical_report["evaluations"]}
    if len(reports) != len(technical_report["evaluations"]) or set(reports) != {member["security_id"] for member in selection["members"]}:
        raise ValueError("qualification must retain every frozen member exactly once")
    result_rows = []
    for member in sorted(selection["members"], key=lambda item: item["security_id"]):
        row = reports[member["security_id"]]
        conditions, all_facts, gaps = [], [], []
        inherited = {item["id"]: item for item in row["eligibility_conditions"]}
        if set(inherited) != ELIGIBILITY_IDS:
            raise ValueError("qualification required fields drifted from existing rules")
        for field, condition_id in FIELD_CONDITIONS.items():
            if field in {"identity", "listed"}:
                condition = deepcopy(inherited[condition_id])
                condition["conflict"] = False
                value = True if condition["status"] == "pass" else False if condition["status"] == "fail" else None
                fact = _fact(member, field, value, selection["target_date"], source_id="frozen_verified_universe",
                    evidence_id=selection["content_hash"], observed_at=selection["cutoff_at"], fetched_at=None,
                    completeness_scope={"kind": "frozen_selection_identity", "security_ids": [member["security_id"]]},
                    reason=condition["reason"], verified=value is not None,
                    proof={"selection_id": selection["selection_id"], "selection_content_hash": selection["content_hash"],
                        "source_business_date": None, "listing_date": member.get("listing_date"), "board": member.get("board")}, mode=selection["mode"])
                all_facts.append(fact)
            else:
                facts = [deepcopy(fact) for fact in field_facts["facts"] if fact["security_id"] == member["security_id"] and fact["field"] == field]
                inherited_fact = _inherited_risk_fact(member, row, technical_report, field, cutoff)
                if inherited_fact:
                    facts.append(inherited_fact)
                if not facts:
                    facts = [_fact(member, field, None, selection["target_date"], reason="目标日字段尚无可验证来源事实", mode=selection["mode"])]
                known = {fact["value"] for fact in facts if fact["verified"] and fact["value"] is not None}
                conflict = len(known) > 1 or any(fact["conflict"] for fact in facts)
                value = next(iter(known)) if len(known) == 1 and not conflict else None
                reason = "适用日期和字段的来源冲突，保持unknown" if conflict else "；".join(sorted({fact["reason"] for fact in facts}))
                condition = {"id": condition_id, "label": inherited[condition_id]["label"],
                    "status": "unknown" if value is None else "fail" if value else "pass", "reason": reason, "conflict": conflict}
                all_facts += facts
            condition["required"] = field != "delisting_period" or require_delisting_check
            if not condition["required"]:
                condition["evidence_status"] = condition["status"]
                condition["status"] = "not_required"
                condition["policy_reason"] = "本期策略不将退市整理期状态作为候选资格条件；保留已有来源事实，不据此判定通过或排除。"
            conditions.append(condition)
            if condition["required"] and condition["status"] == "unknown":
                gaps.append({"field": field, "reason": condition["reason"]})
        status = _combined([condition for condition in conditions if condition["required"]], technical=False)
        queue = "not_triggered" if row["technical_status"] != "pass" else "excluded" if status == "fail" else "diagnostic_pending" if status == "pending" else "local_evidence_preparation"
        result_rows.append({"security_id": member["security_id"], "symbol": _symbol(member), "name": member.get("name"),
            "board": member["board"], "eligibility_status": status, "conditions": conditions, "facts": all_facts, "gaps": gaps,
            "rejected_facts": [fact for fact in all_facts if not fact["verified"]],
            "technical_status": row["technical_status"], "technical_requirements": deepcopy(row["technical_conditions"]),
            "exclusion_reasons": [condition["reason"] for condition in conditions if condition["status"] == "fail"],
            "material_queue_status": queue, "purpose": selection.get("purpose", "production"),
            "production_eligible": selection.get("purpose", "production") == "production",
            "formal_candidate": False, "model_queue_eligible": False})
    counts = Counter(row["eligibility_status"] for row in result_rows)
    result = {"schema_version": RESULT_SCHEMA, "selection_id": selection["selection_id"], "selection_content_hash": selection["content_hash"],
        "target_date": selection["target_date"], "mode": selection["mode"], "purpose": selection.get("purpose", "production"),
        "production_eligible": selection.get("purpose", "production") == "production", "market_scope": "sse_szse_a", "research_mode": "sector_first",
        "technical_result_hash": technical_report["result_hash"], "field_facts_hash": field_facts["content_hash"], "cutoff_at": cutoff.isoformat(),
        "historical_reconstruction": cutoff.date().isoformat() > selection["target_date"],
        "eligibility_policy": {"require_delisting_check": require_delisting_check},
        "required_fields": [field for field in FIELD_CONDITIONS if field != "delisting_period" or require_delisting_check],
        "evaluations": result_rows, "counts": {"stock_count": len(result_rows), **{key + "_count": counts[key] for key in ("pass", "fail", "pending")},
            "formal_candidate_count": 0, "model_queue_count": 0}, "model_calls": 0, "model_tokens": 0,
        "field_facts_file": deepcopy(field_facts.file_ref), "source_health": deepcopy(field_facts["source_health"]),
        "source_date_notice": "源业务日期为空时保留null；同日完整观察快照按既有资格规则适用，不声称源发布了精确业务时间"}
    result["content_hash"] = digest(result)
    return result
