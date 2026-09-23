"""Selected-only history integration tests; synthetic data stays offline_test."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from ashare_daily import sector_history as history
from ashare_daily.calendar import _digest
from ashare_daily.providers import baostock
from ashare_daily.providers import sina_history as sina
from ashare_daily.sector_selection import digest
from test_sina_history import http, raw_rows

DAY = "2026-09-10"
DAYS = ("2026-09-09", DAY)


def config():
    return {"database": "offline_test/market.sqlite3", "calendar_cache": "offline_test/calendar",
        "output_directory": "offline_test/sectors", "target_trading_days": 2, "pause_seconds": 0,
        "sina": {"enabled": True, "user_authorized": True, "purpose": "personal_noncommercial_local_research", "llm_export": False,
            "permission_status": "approved", "permission_basis": "explicit offline fixture permission",
            "permitted_storage": True, "permitted_automated_access": True, "upstream_grant_status": "unconfirmed"}}


def member(index=1):
    return {"security_id": "synthetic-selected-" + str(index), "code": str(688000 + index), "exchange": "SSE", "board": "star",
        "metadata_verified": True, "security_type": "ordinary_a", "discovery_classification": "ordinary_a",
        "provenance_mode": "offline_test", "name": "synthetic", "listing_date": "2020-01-01", "sector_ids": ["industry:a", "industry:b"],
        "statuses": {key: {"value": None, "unknown_reason": "not_reported"} for key in ("st", "suspended", "delisting_period")}}


def freeze(cfg, members=None, status="selected", verified=True, cutoff=None):
    members = [member()] if members is None else members
    value = {"schema_version": "f2s1-selection-v1", "mode": "offline_test", "market_scope": "sse_szse_a",
        "research_mode": "sector_first", "target_date": DAY, "cutoff_at": cutoff or DAY + "T21:00:00+08:00",
        "selection_status": status, "selection_verified": verified, "config_hash": digest(cfg),
        "members": members, "selected_security_count": len(members), "universe_count": 10000,
        "selected_sectors": [{"sector_id": "industry:a"}], "universe_snapshot_id": "offline-universe"}
    value["content_hash"] = digest(value)
    value["selection_id"] = "sector-" + DAY + "-" + value["content_hash"][:20]
    return value


def calendar(root, cfg):
    params = {"start_date": DAYS[0], "end_date": DAY}
    fields = baostock.expected_fields("calendar", params)
    rows = [{"calendar_date": day, "is_trading_day": "1"} for day in DAYS]
    response = baostock.base_result("calendar", params)
    response.update(ok=True, status="ok", error_code="0", fields=fields, rows=rows,
        raw_hash=baostock.raw_hash(fields, rows), fetched_at=DAY + "T21:00:00+08:00",
        provenance_mode="offline_test", login={"ok": True, "error_code": "0", "error_msg": "success"})
    packet = {"schema_version": "f1-calendar-cache-v1", "provider": "baostock", "mode": "offline_test",
        "first_seen_at": response["fetched_at"], "response": response}
    packet["content_hash"] = _digest(packet)
    path = root / cfg["calendar_cache"] / "synthetic-calendar.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(packet), encoding="utf-8")


def source_factory(monkeypatch, calls, *, interrupt_at=None):
    count = 0
    monkeypatch.setattr(sina, "decode_pinned", lambda encoded: [{"date": day, "open": "10", "high": "11", "low": "9",
        "close": "10.5", "volume": "1000", "amount": "10000"} for day in DAYS])
    def make(directory, cfg, mode):
        nonlocal count
        def transport(url):
            nonlocal count
            count += 1
            if count == interrupt_at:
                raise KeyboardInterrupt()
            calls.append(url)
            stamp = datetime.now(sina.SHANGHAI).isoformat()
            if url.endswith("qfq.js"):
                symbol = url.split("/company/")[1].split("/")[0]
                body = "var " + symbol + 'qfq={"total":1,"data":[{"d":"1900-01-01","f":"2"}]};'
            elif "var%20_" in url:
                callback = url.split("var%20_")[1].split("=/")[0]
                body = "var _" + callback + "=(" + json.dumps(raw_rows(DAYS)) + ");"
            else:
                symbol = url.split("/company/")[1].split("/")[0]
                body = 'var KLC_K2_' + symbol + '="ABC";'
            return http(url, body, stamp=stamp)
        return sina.SinaHistoryProvider(directory, permission=cfg["sina"], mode=mode, transport=transport, pause_seconds=0,
            max_requests=min(2000, cfg.get("max_history_requests", 2000)))
    monkeypatch.setattr(history, "_make_provider", make)


def db_counts(root, cfg):
    with sqlite3.connect((root / cfg["database"]).as_uri() + "?mode=ro", uri=True) as connection:
        return {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in ("f2_bar_versions", "f2_adjustment_windows", "f2_batches")}


def test_dynamic_union_exceeds_100_without_network_or_db_creation(tmp_path, monkeypatch):
    cfg = config()
    calendar(tmp_path, cfg)
    selection = freeze(cfg, [member(index) for index in range(1, 152)])
    monkeypatch.setattr(history, "_make_provider", lambda *a: pytest.fail("dry run started a provider"))
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    result = history.prepare_history(tmp_path, selection, cfg, dry_run=True)
    assert result["denominator"] == 151 and len(result["plan"]["tasks"]) == 151
    assert result["network_requests"] == result["database_writes"] == 0
    assert not (tmp_path / cfg["database"]).exists()
    assert before == sorted(str(p) for p in tmp_path.rglob("*"))


def test_request_budget_keeps_entire_union_pending_and_preserves_raw_checkpoint(tmp_path, monkeypatch):
    cfg, calls = config(), []
    cfg["max_history_requests"] = 1
    calendar(tmp_path, cfg)
    selection = freeze(cfg, [member(1), member(2)])
    source_factory(monkeypatch, calls)
    result = history.prepare_history(tmp_path, selection, cfg)
    assert len(calls) == result["metrics"]["requests"] == 1
    assert result["metrics"]["network_requests"] == 0
    assert result["denominator"] == result["pending_count"] == 2
    assert result["acquisition_complete_count"] == 0 and not result["research_ready"]
    assert result["metrics"]["history_requests_outside_selection"] == 0
    assert result["stop_reason"] is not None
    assert db_counts(tmp_path, cfg)["f2_bar_versions"] == len(DAYS)
    assert len(result["details"]) == 2


def test_same_security_overlap_has_one_raw_and_one_factor_then_zero_repeat(tmp_path, monkeypatch):
    cfg, calls = config(), []
    calendar(tmp_path, cfg)
    selection = freeze(cfg)
    source_factory(monkeypatch, calls)
    first = history.prepare_history(tmp_path, selection, cfg)
    assert len(calls) == 2 and first["metrics"]["requests"] == 2
    assert first["metrics"]["network_requests"] == 0  # The fixture never claims online verification.
    assert first["details"][0]["sector_ids"] == ["industry:a", "industry:b"]
    assert first["details"][0]["risk_unknown"] is True
    assert first["adjustment_ready_count"] == 1 and first["history_ready_count"] == 0  # STAR lacks amount.
    assert first["acquisition_complete_count"] == 1 and not first["research_ready"]
    counts = db_counts(tmp_path, cfg)
    second = history.prepare_history(tmp_path, selection, cfg)
    assert second["metrics"]["requests"] == 0 and len(calls) == 2
    assert counts == db_counts(tmp_path, cfg)
    assert second["metrics"]["history_requests_outside_selection"] == 0
    assert second["cumulative_metrics"]["requests"] == 2 and second["cumulative_metrics"]["network_requests"] == 0
    assert second["cumulative_metrics"]["physical_source_rows"] == 2
    assert second["history_fetched_count"] == 1
    before = sorted((str(p), p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file())
    report = history.report_history(tmp_path, selection, cfg)
    assert report["details"] == second["details"]
    assert report["metrics"] == second["metrics"] and report["cumulative_metrics"] == second["cumulative_metrics"]
    assert before == sorted((str(p), p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file())


def test_interrupted_after_raw_resumes_only_factor_and_retains_version(tmp_path, monkeypatch):
    cfg, calls = config(), []
    calendar(tmp_path, cfg)
    selection = freeze(cfg)
    source_factory(monkeypatch, calls, interrupt_at=2)
    first = history.prepare_history(tmp_path, selection, cfg)
    assert first["stop_reason"] == "interrupted_pending_resume" and first["pending_count"] == 1
    assert db_counts(tmp_path, cfg)["f2_bar_versions"] == 2
    source_factory(monkeypatch, calls)
    second = history.prepare_history(tmp_path, selection, cfg)
    assert second["acquisition_complete_count"] == 1
    assert len(calls) == 2 and calls[-1].endswith("qfq.js")
    assert db_counts(tmp_path, cfg)["f2_bar_versions"] == 2


def test_members_leave_cache_is_retained_new_union_gets_only_new_member(tmp_path, monkeypatch):
    cfg, calls = config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    old = freeze(cfg)
    history.prepare_history(tmp_path, old, cfg)
    initial = db_counts(tmp_path, cfg)
    newer = freeze(cfg, [member(2)])
    result = history.prepare_history(tmp_path, newer, cfg)
    assert len(calls) == 4 and all("688002" in url for url in calls[2:])
    assert db_counts(tmp_path, cfg)["f2_bar_versions"] == initial["f2_bar_versions"] + 2
    assert result["denominator"] == 1 and history.report_history(tmp_path, old, cfg)["denominator"] == 1


def test_new_selection_reuses_exact_cached_source_window(tmp_path, monkeypatch):
    cfg, calls = config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    first = freeze(cfg)
    history.prepare_history(tmp_path, first, cfg)
    other = member()
    other["sector_ids"] = ["industry:c"]
    second = history.prepare_history(tmp_path, freeze(cfg, [other]), cfg)
    assert len(calls) == 2 and second["metrics"]["requests"] == 0
    assert second["metrics"]["cache_reused_securities"] == 1


@pytest.mark.parametrize("status,verified", [("no_matching_sectors", True), ("selection_blocked", False)])
def test_zero_selection_or_blocked_does_not_start_history(tmp_path, monkeypatch, status, verified):
    cfg = config()
    selection = freeze(cfg, [], status, verified)
    monkeypatch.setattr(history, "_make_provider", lambda *a: pytest.fail("source must remain unconstructed"))
    result = history.prepare_history(tmp_path, selection, cfg)
    assert result["status"] == status and result["metrics"]["requests"] == 0
    assert not (tmp_path / cfg["database"]).exists()


def test_untrusted_calendar_and_time_budget_do_not_shrink_denominator(tmp_path, monkeypatch):
    cfg = config()
    selection = freeze(cfg, [member(1), member(2)])
    monkeypatch.setattr(history, "_make_provider", lambda *a: pytest.fail("no history request expected"))
    result = history.prepare_history(tmp_path, selection, cfg, dry_run=True)
    assert result["plan"]["status"] == "calendar_blocked" and result["denominator"] == 2
    calendar(tmp_path, cfg)
    result = history.prepare_history(tmp_path, selection, cfg, max_seconds=0)
    assert result["denominator"] == result["pending_count"] == 2
    assert result["stop_reason"] == "time_budget_exhausted"


def test_calendar_blocked_attempt_can_resume_same_selection_when_cache_arrives(tmp_path, monkeypatch):
    cfg = config()
    selection = freeze(cfg)
    monkeypatch.setattr(history, "_make_provider", lambda *a: pytest.fail("budget prevents history"))
    blocked = history.prepare_history(tmp_path, selection, cfg)
    assert blocked["status"] == "calendar_blocked" and blocked["pending_count"] == 1
    assert not (tmp_path / cfg["database"]).exists()
    blocked_path = __import__('pathlib').Path(blocked["plan_path"])
    blocked_hash = hashlib.sha256(blocked_path.read_bytes()).hexdigest()
    assert history.report_history(tmp_path, selection, cfg)["status"] == "calendar_blocked"
    calendar(tmp_path, cfg)
    resumed = history.prepare_history(tmp_path, selection, cfg, max_seconds=0)
    assert resumed["status"] == "history_partial" and resumed["pending_count"] == 1
    assert hashlib.sha256(blocked_path.read_bytes()).hexdigest() == blocked_hash


def test_selection_tampering_changed_config_and_test_research_path_rejected(tmp_path):
    cfg = config()
    selection = freeze(cfg)
    tampered = deepcopy(selection)
    tampered["members"][0]["code"] = "688002"
    with pytest.raises(ValueError, match="hash"):
        history.prepare_history(tmp_path, tampered, cfg, dry_run=True)
    with pytest.raises(ValueError, match="config"):
        history.prepare_history(tmp_path, selection, {**cfg, "target_trading_days": 3}, dry_run=True)
    with pytest.raises(ValueError, match="research"):
        cfg["database"] = "research/market.sqlite3"
        history.prepare_history(tmp_path, freeze(cfg), cfg, dry_run=True)


def test_cache_fact_revision_does_not_change_frozen_report(tmp_path, monkeypatch):
    cfg, calls = config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    selection = freeze(cfg)
    history.prepare_history(tmp_path, selection, cfg)
    before = history.report_history(tmp_path, selection, cfg)
    with sqlite3.connect(tmp_path / cfg["database"]) as db:
        db.execute("UPDATE f2_bar_current SET fact_hash='not-used-by-frozen-report'")
    after = history.report_history(tmp_path, selection, cfg)
    assert before == after


def test_selected_nonordinary_or_bse_identity_rejected_before_source(tmp_path):
    cfg = config()
    for changes in ({"security_type": "cdr"}, {"board": "bse", "exchange": "BSE"}, {"metadata_verified": False}):
        with pytest.raises(ValueError):
            history.prepare_history(tmp_path, freeze(cfg, [{**member(), **changes}]), cfg, dry_run=True)


def test_correction_cannot_pair_new_raw_with_prior_adjusted_anchor(tmp_path, monkeypatch):
    cfg, calls = config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    selection = freeze(cfg)
    first = history.prepare_history(tmp_path, selection, cfg)
    window_id = first["details"][0]["adjustment_window_id"]
    with sqlite3.connect(tmp_path / cfg["database"]) as db:
        sid, provider, day, old_hash, stamp, raw = db.execute("SELECT * FROM f2_bar_versions LIMIT 1").fetchone()
        corrected = json.loads(raw)
        corrected["close"] = "10.6"
        signature = digest(corrected)
        db.execute("INSERT INTO f2_bar_versions VALUES(?,?,?,?,?,?)", (sid, provider, day, signature, stamp, json.dumps(corrected)))
        db.execute("UPDATE f2_bar_current SET fact_hash=? WHERE security_id=? AND trade_date=?", (signature, sid, day))
    newer_member = member()
    newer_member["sector_ids"] = ["industry:corrected-selection"]
    plan = history.prepare_history(tmp_path, freeze(cfg, [newer_member]), cfg, dry_run=True)["plan"]
    task = plan["tasks"][0]
    assert task["adjustment_window_id"] is None and task["status"] == "pending"
    assert task["cached_raw_response"] is None
    assert history.report_history(tmp_path, selection, cfg)["details"][0]["adjustment_window_id"] == window_id


def test_unknown_trading_status_does_not_hide_valid_numerical_quote(tmp_path, monkeypatch):
    cfg, calls = config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    selection = freeze(cfg, [{**member(), "board": "sse_main", "code": "600001"}])
    result = history.prepare_history(tmp_path, selection, cfg)
    assert result["details"][0]["unknown_trading_status"] is True
    checked = result["details"][0]
    assert checked["valid_target_quote"] and checked["unknown_trading_status"]
    assert checked["risk_unknown"] and not checked["research_ready"]


def halt_supplement():
    return {"suspended": {"value": True, "verified": True, "source": "baostock", "evidence_id": "synthetic-source-status",
        "as_of_date": DAY, "full_day": True, "effective_from": DAY, "effective_to": DAY,
        "source_path": "offline_test/frozen-source.json", "source_file_hash": "1" * 64,
        "observed_at": "2026-09-11T18:00:00+08:00", "historical_reconstruction": True}}


def test_dated_supplement_preserves_risks_and_history_union_but_excludes_halt_from_quotes(tmp_path, monkeypatch):
    cfg, calls = config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    selected = [{**member(1), "board": "sse_main", "code": "600001", "supplemental_status_evidence": halt_supplement()},
                {**member(2), "board": "sse_main", "code": "600002"}]
    original = deepcopy(selected)
    selection = freeze(cfg, selected, cutoff="2026-09-11T21:00:00+08:00")
    result = history.prepare_history(tmp_path, selection, cfg)
    assert selected == original and result["denominator"] == 2
    assert result["confirmed_full_day_halted_count"] == 1
    assert result["expected_target_quotes"] == result["valid_target_quotes"] == 1
    assert len(calls) == 4  # Both selected securities retain their complete history task.
    assert result["metrics"]["history_requests_outside_selection"] == 0
    halted, ordinary = result["details"]
    assert halted["confirmed_suspended"] and halted["supplemental_halt_verified"]
    assert not halted["valid_target_quote"] and not halted["target_quote_expected"]
    assert ordinary["valid_target_quote"] and ordinary["target_quote_expected"]
    assert halted["raw_facts_ready"] and halted["adjustment_ready"]
    assert halted["expected_history_dates"] == len(DAYS)
    assert halted["risk_states"] == selected[0]["statuses"]
    assert halted["risk_unknown"] and halted["unknown_trading_status"] and not halted["research_ready"]
    assert halted["supplemental_status_evidence"] == halt_supplement()
    assert halted["supplemental_status_evidence"]["suspended"]["historical_reconstruction"] is True
    checked = history.report_history(tmp_path, selection, cfg)
    assert checked["details"] == result["details"]
    repeated = history.prepare_history(tmp_path, selection, cfg)
    assert repeated["metrics"]["requests"] == 0 and len(calls) == 4
    assert repeated["confirmed_full_day_halted_count"] == 1


@pytest.mark.parametrize("change", [
    {"as_of_date": "2026-09-09"}, {"verified": False}, {"full_day": False},
    {"effective_to": "2026-09-09"}, {"source_file_hash": "not-a-hash"},
    {"observed_at": "2099-01-01T21:00:00+08:00"},
    {"observed_at": "2026-09-11T22:00:00+08:00"},
    {"observed_at": "2026-09-11T18:00:00"}, {"historical_reconstruction": False},
])
def test_invalid_or_late_supplement_never_removes_expected_target_quote(tmp_path, monkeypatch, change):
    cfg, calls = config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    evidence = halt_supplement()
    evidence["suspended"].update(change)
    selected = [{**member(), "board": "sse_main", "code": "600001", "supplemental_status_evidence": evidence}]
    result = history.prepare_history(tmp_path, freeze(cfg, selected, cutoff="2026-09-11T21:00:00+08:00"), cfg)
    detail = result["details"][0]
    assert not detail["confirmed_suspended"] and not detail["supplemental_halt_verified"]
    assert result["expected_target_quotes"] == 1 and detail["valid_target_quote"]
    assert detail["supplemental_status_issues"] and detail["risk_unknown"]
    assert detail["supplemental_status_evidence"] == evidence


def test_old_plan_without_supplement_remains_readable(tmp_path, monkeypatch):
    cfg, calls = config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    selection = freeze(cfg)
    result = history.prepare_history(tmp_path, selection, cfg)
    path = tmp_path / cfg["output_directory"] / selection["selection_id"] / "history/history_fetch_plan.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy.pop("cutoff_at")
    for task in legacy["tasks"]:
        task.pop("supplemental_status_evidence")
    legacy.pop("content_hash")
    path.write_text(json.dumps(history._seal(legacy)), encoding="utf-8")
    reread = history.report_history(tmp_path, selection, cfg)
    assert reread["details"] == result["details"] and reread["denominator"] == 1


def validation_config(readonly=None):
    cfg = config()
    cfg.update(purpose="engineering_validation", production_eligible=False,
        output_directory="outputs/engineering_validation/f3s", database="data/engineering_validation/f3s/market.sqlite3",
        history_lock_file="data/engineering_validation/f3s/history.lock")
    if readonly:
        cfg["readonly_cache_database"] = readonly
    return cfg


def validation_freeze(cfg, members=None):
    value = freeze(cfg, members)
    value.update(schema_version="f3s-validation-selection-v1", purpose="engineering_validation", production_eligible=False,
        source_selection_id="synthetic-verified-source-selection", automatic_selection=False)
    value.pop("content_hash")
    value.pop("selection_id")
    value["content_hash"] = digest(value)
    value["selection_id"] = "validation-sector-" + DAY + "-" + value["content_hash"][:20]
    return value


def test_engineering_history_uses_same_core_and_isolated_store(tmp_path, monkeypatch):
    cfg, calls = validation_config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    selection = validation_freeze(cfg)
    result = history.prepare_history(tmp_path, selection, cfg)
    assert result["purpose"] == "engineering_validation" and result["production_eligible"] is False
    assert len(calls) == 2 and result["metrics"]["requests"] == 2
    assert (tmp_path / cfg["database"]).is_file()
    assert not (tmp_path / "data/research/market.sqlite3").exists()
    exported = history.screening_history_inputs(tmp_path, selection, cfg)
    security = exported["securities"][member()["security_id"]]
    assert len(security["raw_records"]) == len(DAYS) and security["expected_dates"] == list(DAYS)
    assert security["adjustment_window"]["raw_fact_hashes"] == {r["trade_date"]: r["fact_hash"] for r in security["raw_records"]}
    assert exported["file_refs"] and exported["production_eligible"] is False
    assert all(r["first_seen_at"] for r in security["raw_records"])


def test_engineering_readonly_cache_replay_never_writes_original_store(tmp_path, monkeypatch):
    original_cfg, calls = config(), []
    calendar(tmp_path, original_cfg)
    source_factory(monkeypatch, calls)
    original = freeze(original_cfg)
    history.prepare_history(tmp_path, original, original_cfg)
    database = tmp_path / original_cfg["database"]
    original_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    cfg = validation_config(original_cfg["database"])
    selection = validation_freeze(cfg)
    monkeypatch.setattr(history, "_make_provider", lambda *a: pytest.fail("complete cache started network"))
    result = history.prepare_history(tmp_path, selection, cfg)
    assert result["metrics"]["requests"] == result["metrics"]["network_requests"] == 0
    assert result["cache_reused_count"] == result["acquisition_complete_count"] == 1
    assert not (tmp_path / cfg["database"]).exists()
    exported = history.screening_history_inputs(tmp_path, selection, cfg)
    assert exported["securities"][member()["security_id"]]["raw_database"] == str(database)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == original_hash


@pytest.mark.parametrize("key,value", [
    ("database", "data/research/market.sqlite3"), ("output_directory", "outputs/research/sector_first"),
    ("history_lock_file", "state/sector_history.lock"), ("production_eligible", True),
])
def test_engineering_purpose_cannot_write_production_paths(tmp_path, key, value):
    cfg = validation_config()
    cfg[key] = value
    with pytest.raises(ValueError):
        history.prepare_history(tmp_path, validation_freeze(cfg), cfg, dry_run=True)
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_production_purpose_cannot_consume_engineering_path(tmp_path):
    cfg = config()
    cfg.update(database="data/engineering_validation/f3s/market.sqlite3")
    with pytest.raises(ValueError, match="production history"):
        history.prepare_history(tmp_path, freeze(cfg), cfg, dry_run=True)


def test_partial_physical_window_replays_without_network_and_keeps_every_missing_date(tmp_path, monkeypatch):
    cfg, calls = validation_config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    monkeypatch.setattr(sina, "decode_pinned", lambda encoded: [{"date": DAY, "open": "10", "high": "11", "low": "9",
        "close": "10.5", "volume": "1000", "amount": "10000"}])
    selection = validation_freeze(cfg, [{**member(), "code": "600001", "board": "sse_main"}])
    first = history.prepare_history(tmp_path, selection, cfg)
    assert first["pending_count"] == 1 and len(calls) == 2
    counts = db_counts(tmp_path, cfg)
    monkeypatch.setattr(history, "_make_provider", lambda *a: pytest.fail("unchanged physical gap window retried network"))
    repeated = history.prepare_history(tmp_path, selection, cfg)
    assert repeated["metrics"]["requests"] == repeated["metrics"]["network_requests"] == 0
    assert repeated["metrics"]["source_windows_replayed_with_gaps"] == 1
    assert repeated["pending_count"] == 1 and repeated["adjustment_ready_count"] == 0
    assert repeated["details"][0]["history_missing_dates"] == [DAYS[0]]
    assert db_counts(tmp_path, cfg) == counts
    packet = history.screening_history_inputs(tmp_path, selection, cfg)["securities"][member()["security_id"]]
    assert packet["adjustment_window"] is None
    diagnostic = packet["diagnostic_adjustment_window"]
    assert diagnostic["complete"] is False and diagnostic["diagnostic_only"] is True
    assert diagnostic["missing_dates"] == [DAYS[0]] and diagnostic["expected_dates"] == list(DAYS)
    assert diagnostic["raw_fact_hashes"] == {row["trade_date"]: row["fact_hash"] for row in packet["raw_records"]}
    assert "risk_states_unknown" in packet["source_limitations"] and "risk_states_unknown" not in packet["issues"]
    assert "history_calendar_dates_missing" in packet["issues"]
    source = diagnostic["observations"][0]["source_response_path"]
    with history._io(Path(source)).open("a", encoding="utf-8") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="hash"):
        history.screening_history_inputs(tmp_path, selection, cfg)


def test_restored_history_reads_registered_sources_and_local_mutable_database(tmp_path, monkeypatch):
    import shutil
    from ashare_daily.operations.backup import _allowed
    original, restored = tmp_path / "source", tmp_path / "restored"
    cfg, calls = validation_config(), []
    calendar(original, cfg)
    source_factory(monkeypatch, calls)
    selection = validation_freeze(cfg)
    history.prepare_history(original, selection, cfg)
    original_database = (original / cfg["database"]).read_bytes()
    shutil.copytree(history._io(original), history._io(restored))
    files = []
    for source in history._io(original).rglob("*"):
        if (source.is_file() and _allowed(source.relative_to(history._io(original)))
                and source.relative_to(history._io(original)).as_posix().startswith(("outputs/engineering_validation/f3s/", "data/engineering_validation/f3s/"))):
            files.append({"path": source.relative_to(history._io(original)).as_posix(), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "size": source.stat().st_size, "kind": "sqlite_backup" if source.suffix == ".sqlite3" else "immutable_file"})
    (restored / "backup-manifest.json").write_text(json.dumps({"schema_version": "m4-backup-v1", "source_project_root": str(original), "files": files}), encoding="utf-8")
    (restored / "restore-path-map.json").write_text(json.dumps({"schema_version": "m4-restore-map-v1", "source_project_root": str(original), "paths": [f["path"] for f in files]}), encoding="utf-8")
    first = history.screening_history_inputs(restored, selection, cfg)
    assert first["securities"][member()["security_id"]]["raw_database"] == str(restored / cfg["database"])
    with sqlite3.connect(restored / cfg["database"]) as db:
        db.execute("PRAGMA user_version=7")  # legitimate mutable restored DB; immutable facts do not change
    second = history.screening_history_inputs(restored, selection, cfg)
    assert first == second
    assert (original / cfg["database"]).read_bytes() == original_database
    reference = next(ref for ref in second["file_refs"] if "/responses/" in ref["path"].replace("\\", "/"))
    with history._io(Path(reference["path"])).open("a", encoding="utf-8") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="SHA256"):
        history.screening_history_inputs(restored, selection, cfg)


def test_source_not_updated_to_target_does_not_permanently_freeze_stale_gap(tmp_path, monkeypatch):
    cfg, calls = validation_config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    monkeypatch.setattr(sina, "decode_pinned", lambda encoded: [{"date": DAYS[0], "open": "10", "high": "11", "low": "9",
        "close": "10.5", "volume": "1000", "amount": "10000"}])
    selection = validation_freeze(cfg, [{**member(), "code": "600001", "board": "sse_main"}])
    history.prepare_history(tmp_path, selection, cfg)
    result = history.prepare_history(tmp_path, selection, cfg)
    assert result["metrics"]["source_windows_replayed_with_gaps"] == 0
    assert result["metrics"]["requests"] == 2 and len(calls) == 4
    assert result["pending_count"] == 1 and result["details"][0]["history_missing_dates"] == [DAY]
