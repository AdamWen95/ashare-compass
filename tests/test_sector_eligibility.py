"""Explicit offline fixtures; no real qualification is inferred from these tests."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

from ashare_daily.providers.baostock import base_result, expected_fields, raw_hash
from ashare_daily.qualification_sources import SSE_URL, SZSE_URL
from ashare_daily.sector_eligibility import (LEGACY_BUNDLE, PACKET_SCHEMA, collect_sector_eligibility,
    evaluate_sector_eligibility, read_field_facts)
from ashare_daily.sector_selection import digest

T = "2026-09-11"
SEEN = "2026-09-14T14:00:00+08:00"
OLD_SEEN = "2026-09-11T11:12:59+08:00"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    path.write_bytes(body)
    return {"path": str(path), "sha256": hashlib.sha256(body).hexdigest()}


def selection(count=1, boards=None):
    members = []
    for i in range(count):
        board = (boards or ["sse_main"])[i % len(boards or ["sse_main"])]
        exchange = "SSE" if board in {"sse_main", "star"} else "SZSE"
        members.append({"security_id": "fixture-security-"+str(i), "code": str(600001+i) if exchange == "SSE" else str(100001+i),
            "exchange": exchange, "board": board, "security_type": "ordinary_a", "metadata_verified": True,
            "name": "OFFLINE_TEST", "listing_date": "2000-01-01", "listing_status": "listed"})
    item = {"schema_version": "f3s-validation-selection-v1", "mode": "offline_test", "purpose": "engineering_validation",
        "production_eligible": False, "automatic_selection": False, "source_selection_id": "sector-fixture",
        "market_scope": "sse_szse_a", "research_mode": "sector_first", "target_date": T, "cutoff_at": OLD_SEEN,
        "members": members, "selected_security_count": len(members), "selection_verified": True}
    signature = digest(item)
    item.update(content_hash=signature, selection_id="validation-sector-"+T+"-"+signature[:20])
    return item


def report(selected):
    item = {"schema_version": "f3s-screening-result-v1", "selection_id": selected["selection_id"], "mode": selected["mode"],
        "purpose": "engineering_validation", "production_eligible": False, "target_date": T, "cutoff_at": OLD_SEEN,
        "evaluations": [{"security_id": member["security_id"], "technical_status": "pass",
            "eligibility_conditions": [{"id": key, "label": key, "reason": "OFFLINE_TEST", "status": "pass" if key in {"identity", "listed"} else "unknown"}
                for key in ("identity", "listed", "not_st", "not_suspended", "not_delisting_period")],
            "technical_conditions": [{"id": "history", "status": "pass"}, {"id": "liquidity", "status": "pass"}]}
            for member in selected["members"]]}
    item["result_hash"] = digest(item)
    return item


def response(parameters, *, st="0", trading="1"):
    result = base_result("history_f2", parameters)
    fields = expected_fields("history_f2", parameters)
    row = {key: "1" for key in fields}
    row.update(date=T, code=parameters["code"], isST=st, tradestatus=trading, adjustflag="3")
    result.update(ok=True, status="ok", error_code="0", error_msg="", fetched_at=SEEN, fields=fields, rows=[row],
        login={"ok": True, "error_code": "0", "error_msg": ""}, raw_hash=raw_hash(fields, [row]),
        verification_kind="offline_test", provenance_mode="offline_test", mode="offline_test")
    return result


def permission(root):
    write(root/"config/sse_szse_market_providers.json", {"provider": "baostock", "permission_status": "approved",
        "permission_basis": "OFFLINE_TEST local only", "model_calls": 0})


def legacy(root, *, market="SH", source_date=None, value_symbols=None):
    sid = "sse_mainboard_delisting" if market == "SH" else "szse_delisting"
    url = SSE_URL if market == "SH" else SZSE_URL
    members = value_symbols or []
    directory = root / Path(LEGACY_BUNDLE).parent
    if market == "SH":
        source_body = {"sqlId": "PL_SSGSXX_FXJSBGPLB", "actionErrors": [], "fieldErrors": {}, "isPagination": "false",
            "queryDate": source_date or "", "result": [{"INSTRUMENT_ID": code[3:], "INSTRUMENT_SHORT": "OFFLINE_TEST"} for code in members],
            "pageHelp": {"total": len(members)}}
    else:
        source_body = [{"metadata": {"tabkey": "tab2", "catalogid": "fxjsb", "name": "退市整理股票", "subname": source_date or "",
            "cols": {"zqdm": "证券代码", "gsjc": "证券简称"}, "recordcount": len(members), "pagecount": 1 if members else 0,
            "pagesize": 20, "pageno": 1}, "error": None,
            "data": [{"zqdm": code[3:], "gsjc": "OFFLINE_TEST"} for code in members]}]
    raw_ref = write(directory/(sid+".raw"), source_body)
    source = {"source_id": sid, "name": "OFFLINE_TEST", "source_url": url, "access_method": "offline_test",
        "field_meaning": "OFFLINE_TEST delisting", "markets": [market], "coverage_scope": "all_mainboard_a_shares",
        "supports_historical_dates": False, "supports_complete_lists": True, "approved_for_local_use": True,
        "usage_limits": "OFFLINE_TEST only", "reviewed_by": "OFFLINE_TEST", "reviewed_at": OLD_SEEN, "review_basis": "OFFLINE_TEST"}
    record = {"evidence_id": "offline-list", "source_id": sid, "kind": "complete_delisting_list", "market": market,
        "source_url": url, "raw_locator": raw_ref["path"], "raw_sha256": raw_ref["sha256"], "content_version": "offline-v1",
        "first_seen_at": OLD_SEEN, "fetched_at": OLD_SEEN, "effective_from": T, "effective_to": T,
        "temporal_basis": "same_date_complete_snapshot", "retrieval_status": "ok" if members else "empty_confirmed",
        "parse_status": "ok", "reviewed_by": "OFFLINE_TEST", "reviewed_at": OLD_SEEN, "review_basis": "OFFLINE_TEST",
        "evidence_excerpt": "OFFLINE_TEST", "assertion_basis": "complete_list", "complete_scope": "all_mainboard_a_shares",
        "expected_total_records": len(members), "total_pages": 1,
        "pages": [{"number": 1, "source_url": url, "raw_locator": raw_ref["path"], "raw_sha256": raw_ref["sha256"],
            "status": "ok", "symbols": members}]}
    bundle = {"schema_version": "eligibility-evidence-v1", "verification_kind": "offline_test", "sources": [source], "records": [record]}
    write(root/LEGACY_BUNDLE, bundle)
    write(directory/"result.json", {"verification_kind": "offline_test", "source_health": [{"source_id": sid, "status": "ok", "target_date": T,
        "requests": [{"role": "page1", "http_status": 200, "url": url, "raw_sha256": raw_ref["sha256"], "fetched_at": OLD_SEEN}]}]})
    return bundle, source_body


def collect(root, selected, *, online=False, transport=None):
    return collect_sector_eligibility(root, selected, output_directory="evidence", cutoff_at=SEEN, online=online, transport=transport)


def test_daily_explicit_probe_subset_keeps_full_denominator_and_stays_in_selection(tmp_path):
    permission(tmp_path)
    selected = selection(count=9)
    called = []
    ids = [selected["members"][8]["security_id"], selected["members"][2]["security_id"]]
    def transport(operation, **parameters):
        called.append(parameters["code"])
        return response(parameters)
    packet = collect_sector_eligibility(tmp_path, selected, output_directory="evidence", cutoff_at=SEEN,
        online=True, transport=transport, probe_security_ids=ids, max_probe_members=9)
    assert called == ["sh.600009", "sh.600003"]
    assert {fact["security_id"] for fact in packet["facts"]} == set(ids)
    result = evaluate_sector_eligibility(selected, report(selected), packet, cutoff_at=SEEN)
    assert result["counts"]["stock_count"] == len(result["evaluations"]) == 9
    assert result["counts"]["pending_count"] == 9
    with pytest.raises(ValueError, match="out-of-selection"):
        collect_sector_eligibility(tmp_path, selected, output_directory="evidence", probe_security_ids=["foreign"])


def test_daily_production_never_imports_engineering_database(tmp_path):
    selected = selection()
    selected.pop("selection_id")
    selected.pop("content_hash")
    selected.update(schema_version="f2s1-selection-v1", purpose="production", production_eligible=True)
    signature = digest(selected)
    selected.update(content_hash=signature, selection_id="sector-"+T+"-"+signature[:20])
    path = tmp_path / "data/engineering_validation/f3s/market.sqlite3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"OFFLINE_TEST: deliberately not a database")
    packet = collect_sector_eligibility(tmp_path, selected, output_directory="evidence", online=False)
    assert packet["facts"] == [] and packet["proofs"] == []


def evaluate(selected, facts):
    return evaluate_sector_eligibility(selected, report(selected), facts, cutoff_at=SEEN)


def test_empty_production_does_not_call_source_or_create_fallback_members(tmp_path):
    selected = selection(0)
    calls = []
    facts = collect(tmp_path, selected, online=True, transport=lambda *a, **k: calls.append(k))
    assert not calls and not facts["facts"]
    result = evaluate(selected, facts)
    assert result["counts"]["stock_count"] == 0 and not result["evaluations"]


def test_all_five_existing_requirements_retained_and_history_liquidity_stay_on_technical_axis(tmp_path):
    selected = selection()
    result = evaluate(selected, collect(tmp_path, selected))
    row = result["evaluations"][0]
    assert result["required_fields"] == ["identity", "listed", "st", "suspended", "delisting_period"]
    assert row["eligibility_status"] == "pending" and {gap["field"] for gap in row["gaps"]} == {"st", "suspended", "delisting_period"}
    assert {condition["id"] for condition in row["technical_requirements"]} == {"history", "liquidity"}
    assert row["material_queue_status"] == "diagnostic_pending"


@pytest.mark.parametrize("st,trading,status", [("0", "1", "pending"), ("1", "1", "fail"), ("0", "0", "fail"), ("", "", "pending")])
def test_dated_bao_fields_fail_before_pending_without_delisting_inference(tmp_path, st, trading, status):
    selected = selection()
    permission(tmp_path)
    packet = collect(tmp_path, selected, online=True, transport=lambda op, **params: response(params, st=st, trading=trading))
    row = evaluate(selected, packet)["evaluations"][0]
    assert row["eligibility_status"] == status
    assert "delisting_period" in {gap["field"] for gap in row["gaps"]}
    assert all(fact["historical_reconstruction"] for fact in packet["facts"])
    assert all(fact["published_at"] is None for fact in packet["facts"])
    assert all(fact["source_business_date"] == T for fact in packet["facts"])
    assert not row["formal_candidate"] and not row["model_queue_eligible"]


def test_same_date_complete_list_reuses_old_resolver_without_inventing_source_business_date(tmp_path):
    selected = selection()
    legacy(tmp_path)
    packet = collect(tmp_path, selected)
    assert len(packet["facts"]) == 1
    fact = packet["facts"][0]
    assert fact["field"] == "delisting_period" and fact["value"] is False
    assert fact["source_business_date"] is None and fact["observed_at"].startswith("2026-09-11T11:12:59")
    assert fact["effective_from"] == fact["effective_to"] == T
    assert fact["completeness_scope"]["kind"] == "same_date_complete_snapshot"


def test_all_required_facts_can_pass_but_engineering_never_enters_model_or_production(tmp_path):
    selected = selection()
    legacy(tmp_path)
    permission(tmp_path)
    packet = collect(tmp_path, selected, online=True, transport=lambda op, **params: response(params))
    result = evaluate(selected, packet)
    assert result["counts"]["pass_count"] == 1
    assert result["evaluations"][0]["material_queue_status"] == "local_evidence_preparation"
    assert result["purpose"] == "engineering_validation" and result["production_eligible"] is False
    assert result["model_calls"] == result["model_tokens"] == result["counts"]["formal_candidate_count"] == 0


@pytest.mark.parametrize("board", ["star", "chinext"])
def test_old_mainboard_list_does_not_certify_other_boards(tmp_path, board):
    selected = selection(boards=[board])
    legacy(tmp_path, market="SH" if board == "star" else "SZ")
    packet = collect(tmp_path, selected)
    fact = packet["facts"][0]
    assert fact["value"] is None and not fact["verified"]
    assert evaluate(selected, packet)["evaluations"][0]["eligibility_status"] == "pending"


@pytest.mark.parametrize("mutation", ["truncated", "stale_source_date", "false_empty", "wrong_market", "stale_effective_date", "partial_page"])
def test_incomplete_wrong_scope_and_stale_lists_never_imply_false(tmp_path, mutation):
    selected = selection()
    bundle, body = legacy(tmp_path)
    if mutation == "truncated": body["pageHelp"]["total"] = 1
    elif mutation == "stale_source_date": body["queryDate"] = "2026-09-10"
    elif mutation == "false_empty": bundle["records"][0]["retrieval_status"] = "empty"
    elif mutation == "wrong_market": bundle["records"][0]["market"] = "SZ"
    elif mutation == "stale_effective_date": bundle["records"][0]["effective_to"] = "2026-09-10"
    elif mutation == "partial_page": bundle["records"][0]["pages"][0]["status"] = "partial"
    if mutation in {"truncated", "stale_source_date"}:
        ref = write(Path(bundle["records"][0]["raw_locator"]), body)
        bundle["records"][0]["raw_sha256"] = ref["sha256"]
        bundle["records"][0]["pages"][0]["raw_sha256"] = ref["sha256"]
        health_path = (tmp_path/LEGACY_BUNDLE).parent/"result.json"
        health = json.loads(health_path.read_text())
        health["source_health"][0]["requests"][0]["raw_sha256"] = ref["sha256"]
        write(health_path, health)
    write(tmp_path/LEGACY_BUNDLE, bundle)
    packet = collect(tmp_path, selected)
    assert not any(fact["value"] is False for fact in packet["facts"])
    assert evaluate(selected, packet)["evaluations"][0]["eligibility_status"] == "pending"


def test_fake_false_with_rehashed_packet_is_rejected_by_source_replay(tmp_path):
    selected = selection()
    packet = collect(tmp_path, selected)
    forged = dict(packet)
    forged["facts"] = [{"security_id": selected["members"][0]["security_id"], "field": "st", "value": False, "verified": True}]
    forged["content_hash"] = digest({key: value for key, value in forged.items() if key != "content_hash"})
    path = tmp_path/"forged.json"
    write(path, forged)
    with pytest.raises(ValueError, match="source replay"):
        read_field_facts(tmp_path, path, selected)
    with pytest.raises(ValueError, match="read_field_facts"):
        evaluate(selected, forged)


def test_in_memory_mutation_cannot_rehash_away_source_verification(tmp_path):
    selected = selection()
    packet = collect(tmp_path, selected)
    packet["source_health"].append({"fake": True})
    packet["content_hash"] = digest({key: value for key, value in packet.items() if key != "content_hash"})
    with pytest.raises(ValueError, match="modified"):
        evaluate(selected, packet)


def test_source_file_tamper_blocks_reimport_even_when_packet_hash_is_unchanged(tmp_path):
    selected = selection()
    legacy(tmp_path)
    packet = collect(tmp_path, selected)
    source = Path(packet["file_refs"][-1]["path"])
    source.write_text("{}")
    with pytest.raises((ValueError, KeyError)):
        read_field_facts(tmp_path, packet.file_ref["path"], selected)


def test_seven_members_are_all_queried_once_and_no_stock_removed(tmp_path):
    selected = selection(7)
    permission(tmp_path)
    calls = []
    def transport(operation, **params):
        calls.append(params)
        return response(params)
    packet = collect(tmp_path, selected, online=True, transport=transport)
    assert len(calls) == 7 and len({params["code"] for params in calls}) == 7
    assert all(params["start_date"] == params["end_date"] == T for params in calls)
    result = evaluate(selected, packet)
    assert result["counts"]["stock_count"] == 7 and result["counts"]["pending_count"] == 7
    assert packet["network_requests"] == 0 and packet["query_attempts"] == 7


def test_source_first_failure_stops_remaining_queries(tmp_path):
    selected = selection(7)
    permission(tmp_path)
    calls = []
    def transport(operation, **params):
        calls.append(params)
        return base_result(operation, params)
    packet = collect(tmp_path, selected, online=True, transport=transport)
    assert len(calls) == 1 and packet["source_health"][-1]["source_stopped"]
    assert evaluate(selected, packet)["counts"]["pending_count"] == 7


def test_research_rejects_test_transport_and_test_response_mode(tmp_path):
    selected = selection()
    selected["mode"] = "research"
    selected.pop("content_hash")
    selected.pop("selection_id")
    hashed = digest(selected)
    selected.update(content_hash=hashed, selection_id="validation-sector-"+T+"-"+hashed[:20])
    with pytest.raises(ValueError, match="test transport"):
        collect_sector_eligibility(tmp_path, selected, output_directory="evidence", online=True, transport=lambda *a: {})


def test_conflicting_valid_dated_responses_stay_pending(tmp_path):
    selected = selection()
    permission(tmp_path)
    first = collect(tmp_path, selected, online=True, transport=lambda op, **params: response(params, st="0"))
    params = first["facts"][0]["proof"]["parameters"]
    other = write(tmp_path/"conflicting-response.json", response(params, st="1"))
    payload = dict(first)
    payload["proofs"] = first["proofs"] + [{"kind": "baostock_response", "security_id": selected["members"][0]["security_id"], "file_ref": other}]
    # The import is built by the same source replay path, never hand-written booleans.
    from ashare_daily.sector_eligibility import _replay
    payload["facts"], payload["file_refs"] = _replay(tmp_path, selected, payload["proofs"], datetime.fromisoformat(SEEN))
    payload["content_hash"] = digest({key: value for key, value in payload.items() if key != "content_hash"})
    path = tmp_path/"conflict.json"
    write(path, payload)
    row = evaluate(selected, read_field_facts(tmp_path, path, selected))["evaluations"][0]
    assert row["eligibility_status"] == "pending"
    assert next(condition for condition in row["conditions"] if condition["id"] == "not_st")["status"] == "unknown"


def test_restore_relocates_reads_without_rewriting_frozen_fact_hashes(tmp_path, monkeypatch):
    import shutil
    import ashare_daily.sector_eligibility as module
    original = tmp_path/"original"
    restored = tmp_path/"restored"
    selected = selection()
    legacy(original)
    permission(original)
    packet = collect(original, selected, online=True, transport=lambda op, **params: response(params))
    shutil.copytree(original, restored)
    def restored_path(locator, *, anchor):
        path = Path(locator)
        return restored/path.relative_to(original) if path.is_relative_to(original) else path
    monkeypatch.setattr(module, "resolve_archived_path", restored_path)
    actual = read_field_facts(restored, packet.file_ref["path"], selected)
    assert actual == packet
    assert actual.file_ref["path"].startswith(str(restored))
    assert evaluate(selected, actual)["counts"]["pass_count"] == 1


def test_new_false_cannot_hide_conflicting_existing_dated_exclusion(tmp_path):
    selected = selection()
    permission(tmp_path)
    facts = collect(tmp_path, selected, online=True, transport=lambda op, **params: response(params, st="0"))
    technical = report(selected)
    technical["evaluations"][0].update(risk_states={"st": True},
        risk_evidence={"st": [{"value": True, "effective_date": T, "evidence_id": "old-frozen-ST", "observed_at": OLD_SEEN}]})
    technical["result_hash"] = digest({key: value for key, value in technical.items() if key != "result_hash"})
    result = evaluate_sector_eligibility(selected, technical, facts, cutoff_at=SEEN)
    row = result["evaluations"][0]
    assert row["eligibility_status"] == "pending"
    condition = next(item for item in row["conditions"] if item["id"] == "not_st")
    assert condition["status"] == "unknown"
    assert {fact["value"] for fact in row["facts"] if fact["field"] == "st"} == {True, False}


def test_existing_known_exclusion_stays_fail_when_other_new_fields_unknown(tmp_path):
    selected = selection()
    facts = collect(tmp_path, selected)
    technical = report(selected)
    technical["evaluations"][0].update(risk_states={"st": True},
        risk_evidence={"st": [{"value": True, "effective_date": T, "evidence_id": "old-frozen-ST", "observed_at": OLD_SEEN}]})
    technical["result_hash"] = digest({key: value for key, value in technical.items() if key != "result_hash"})
    result = evaluate_sector_eligibility(selected, technical, facts, cutoff_at=SEEN)
    assert result["counts"]["fail_count"] == 1
    assert {gap["field"] for gap in result["evaluations"][0]["gaps"]} == {"suspended", "delisting_period"}


def test_runtime_budget_stops_before_request_without_calling_source(tmp_path, monkeypatch):
    import ashare_daily.sector_eligibility as module
    selected = selection(7)
    permission(tmp_path)
    times = iter([0, 2])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    def forbidden(*args, **kwargs):
        pytest.fail("runtime-expired qualification sent a request")
    packet = collect_sector_eligibility(tmp_path, selected, output_directory="deadline", online=True,
        transport=forbidden, cutoff_at=SEEN, max_seconds=1)
    assert packet["query_attempts"] == 0
    assert packet["source_health"][-1]["status"] == "runtime_limit_exhausted"
    assert packet["runtime_limit_seconds"] == 1
    assert evaluate(selected, packet)["counts"]["pending_count"] == 7


@pytest.mark.parametrize("maximum", [0, -1, True, float("inf"), float("nan"), 14401])
def test_invalid_runtime_budget_rejected_before_artifact_creation(tmp_path, maximum):
    with pytest.raises(ValueError, match="runtime limit"):
        collect_sector_eligibility(tmp_path, selection(), output_directory="deadline", max_seconds=maximum)
    assert not list(tmp_path.iterdir())
