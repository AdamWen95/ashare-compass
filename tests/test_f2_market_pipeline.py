"""F2 pipeline integration with real contracts and explicitly isolated fixtures."""
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from ashare_daily.market_foundation import F2MarketStore
from ashare_daily.market_pipeline import run_market, quality_report
from ashare_daily.operations.backup import _io
from ashare_daily.providers.baostock import HISTORY_STOCK_FIELDS, base_result, raw_hash
from ashare_daily.universe import UniverseStore, sync_universe
from test_f1_universe import inputs, row


ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-09-10"
PREVIOUS = "2026-09-09"
FOUR = ("sse_main", "szse_main", "chinext", "star")


def setup_project(tmp_path, *, count=4, extra_cdr=True, scope="sse_szse_a", unknown=True):
    config = json.loads((ROOT / "config/sse_szse_market.json").read_text("utf-8"))
    config.update(scope=scope, universe_config="universe.json", database="offline/market.sqlite3",
                  checkpoint_database="offline/jobs.sqlite3", calendar_cache="offline/calendar",
                  output_directory="offline/output", max_run_seconds=120, recheck_days=2)
    (tmp_path / "market.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "universe.json").write_text(json.dumps({"scope": scope, "database": "offline/universe.sqlite3"}), encoding="utf-8")
    records = [row(index, board=FOUR[index % 4], day=DAY, code=f"{900000 + index:06d}",
                   listing_date=PREVIOUS, statuses={} if unknown else row(index, day=DAY)["statuses"])
               for index in range(count)]
    if extra_cdr:
        records.append(row(999, board="star", day=DAY, code="999999", security_type="cdr", listing_date=PREVIOUS))
    data = inputs(records, day=DAY)
    data["manifests"][0]["coverage_boards"] = list(FOUR)
    with UniverseStore(tmp_path / "offline/universe.sqlite3", mode="offline_test") as store:
        snapshot = sync_universe(store, **data, scope=scope)
    assert snapshot["mode"] == "offline_test" and not snapshot["universe_verified"]
    assert not snapshot["collection_ready"]  # No fixture is relabeled as live evidence.
    (tmp_path / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    return config, snapshot


def calendar(target, cache, **kwargs):
    return {"verified": True, "calendar": {PREVIOUS: True, DAY: True}, "resolved_trade_date": str(target),
            "provenance_mode": "offline_test", "calendar_verified": True, "source_hash": "synthetic-calendar"}


class Source:
    def __init__(self, *, fail_code=None, missing_code=None, suspend_code=None, unknown_code=None,
                 close="10", stamp="2026-09-10T22:00:00+08:00", interrupt_at=None, permission=False):
        self.calls = []
        self.fail_code, self.missing_code, self.suspend_code, self.unknown_code = fail_code, missing_code, suspend_code, unknown_code
        self.close, self.stamp, self.interrupt_at, self.permission = close, stamp, interrupt_at, permission

    def _finished(self, result):
        result["attempts"] = [{"attempt": 1, **{key: result[key] for key in (
            "ok", "status", "error_code", "error_msg", "fetched_at", "elapsed_seconds", "login", "sdk_log")}}]
        return result

    def query(self, operation, **parameters):
        self.calls.append((operation, deepcopy(parameters)))
        if len(self.calls) == self.interrupt_at:
            raise KeyboardInterrupt
        result = base_result(operation, parameters)
        result.update(fetched_at=self.stamp, provenance_mode="offline_test")
        if parameters["code"] == self.fail_code or self.permission:
            result.update(error_code="403" if self.permission else "10002007",
                          status="permission_denied" if self.permission else "unknown", error_msg="synthetic controlled failure")
            return self._finished(result)
        rows = []
        for day in (PREVIOUS, DAY):
            if not parameters["start_date"] <= day <= parameters["end_date"]:
                continue
            if parameters["code"] == self.missing_code and day == DAY:
                continue
            suspended = parameters["code"] == self.suspend_code
            unknown = parameters["code"] == self.unknown_code
            rows.append({"date": day, "code": parameters["code"], "open": "10", "high": "11", "low": "9",
                         "close": self.close, "preclose": "10", "volume": "" if suspended else "1000",
                         "amount": "" if suspended else "10000", "adjustflag": "2" if parameters["adjustment_mode"] == "forward_adjusted" else "3",
                         "tradestatus": "" if unknown else "0" if suspended else "1", "isST": "" if unknown else "0"})
        result.update(ok=True, status="ok" if rows else "empty_confirmed", error_code="0", rows=rows,
                      fields=HISTORY_STOCK_FIELDS, raw_hash=raw_hash(HISTORY_STOCK_FIELDS, rows),
                      login={"ok": True, "error_code": "0", "error_msg": "success"})
        return self._finished(result)


def run(tmp_path, source=None, **updates):
    return run_market(project_root=tmp_path, config_path="market.json", target_date=DAY,
                      mode="offline_test", client=source or Source(), calendar_resolver=calendar, **updates)


def facts(tmp_path):
    with sqlite3.connect(tmp_path / "offline/market.sqlite3") as connection:
        return connection.execute("SELECT COUNT(*) FROM f2_bar_versions").fetchone()[0]


def structural(result):
    return result["structural_market_complete"]


def test_default_snapshot_lookup_uses_target_date_and_offline_contract_never_becomes_live(tmp_path):
    setup_project(tmp_path)
    result, code = run(tmp_path)
    assert code == 0 and structural(result)
    assert result["mode"] == "offline_test" and result["market_complete"] is False
    assert result["f3_ready"] is False and result["research_ready"] is False
    assert result["model_calls"] == 0 and result["point_in_time_adjustment_verified"] is False


def test_full_four_board_cohort_exceeds_100_and_excludes_cdr_without_dropping_unknown_risks(tmp_path):
    _, snapshot = setup_project(tmp_path, count=124)
    source = Source()
    result, code = run(tmp_path, source)
    assert code == 0 and structural(result)
    assert snapshot["discovered_unique"] == 125 and result["denominator"] == 124
    assert set(result["board_coverage"]) == set(FOUR)
    assert all(value["expected_target"] == 31 for value in result["board_coverage"].values())
    assert result["totals"]["unknown_delisting_period"] == 124
    assert result["totals"]["research_ready"] == 0
    assert len(source.calls) == 248
    assert all(parameters["code"] != "sh.999999" for _, parameters in source.calls)
    assert facts(tmp_path) == 248


def test_explicit_frozen_snapshot_path_agrees_with_ledger(tmp_path):
    setup_project(tmp_path)
    result, code = run(tmp_path, universe_snapshot_path="snapshot.json")
    assert code == 0 and result["denominator"] == 4
    snapshot_path = tmp_path / "snapshot.json"
    edited = json.loads(snapshot_path.read_text("utf-8"))
    edited["ordinary_a_count"] = 3
    snapshot_path.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        run(tmp_path, universe_snapshot_path="snapshot.json")


def test_unknown_row_status_saves_facts_and_remains_unknown(tmp_path):
    setup_project(tmp_path)
    result, _ = run(tmp_path, Source(unknown_code="sh.900000"))
    assert result["denominator"] == 4
    assert result["totals"]["unknown_st"] == 1 and result["totals"]["unknown_trading_status"] == 1
    assert facts(tmp_path) == 8
    assert result["research_ready"] is False
    assert not structural(result) and result["tasks"]["failed"] == 2
    assert result["totals"]["valid_target"] == 3


def test_unknown_trade_status_is_retried_and_can_be_repaired_without_duplicate_facts(tmp_path):
    setup_project(tmp_path)
    first, _ = run(tmp_path, Source(unknown_code="sh.900000"))
    repaired_source = Source(stamp="2026-09-10T23:00:00+08:00")
    repaired, code = run(tmp_path, repaired_source, operation="resume", job_id=first["job_id"])
    assert code == 0 and structural(repaired)
    assert len(repaired_source.calls) == 2  # Both incomplete windows, no restart of other boards.
    assert repaired["totals"]["unknown_trading_status"] == 0
    assert facts(tmp_path) == 10  # Eight original facts plus two real status corrections.


@pytest.mark.parametrize("failure", ["network", "missing"])
def test_failed_or_stale_symbol_is_not_deleted_from_global_or_board_denominator(tmp_path, failure):
    setup_project(tmp_path)
    source = Source(**({"fail_code": "sh.900003"} if failure == "network" else {"missing_code": "sh.900003"}))
    result, code = run(tmp_path, source)
    assert code == 2 and not structural(result)
    assert result["denominator"] == 4
    assert result["board_coverage"]["star"]["expected_target"] == 1
    assert result["board_coverage"]["star"]["valid_target"] == 0
    assert result["totals"]["missing_target"] == 1
    assert result["tasks"]["failed"] == 2
    problems = json.loads(Path(result["problems_path"]).read_text("utf-8"))
    star = next(item for item in problems if item["board"] == "star")
    assert DAY in star["missing_dates"] and len(star["task_issues"]) == 2
    assert all(Path(item["response_path"]).is_file() for item in star["task_issues"])


def test_suspension_keeps_missing_value_flags_but_has_its_own_quote_denominator(tmp_path):
    setup_project(tmp_path)
    result, code = run(tmp_path, Source(suspend_code="sh.900003"))
    assert code == 0 and structural(result)
    assert result["totals"]["confirmed_suspended"] == 1
    assert result["totals"]["valid_target"] == 3 and result["totals"]["missing_target"] == 0
    assert result["board_coverage"]["star"]["history_valid_dates"] == 0
    assert result["board_coverage"]["star"]["history_suspended_dates"] == 2
    with sqlite3.connect(tmp_path / "offline/market.sqlite3") as connection:
        rows = [json.loads(row[0]) for row in connection.execute("SELECT payload_json FROM f2_bar_versions")]
    suspended = [item for item in rows if item["tradestatus"] is False]
    assert len(suspended) == 2 and all(item["volume_shares"] is None for item in suspended)
    assert all("missing_volume_shares" in item["quality_flags"] for item in suspended)


def test_interrupt_resume_skips_completed_requests_and_identical_inputs_do_not_duplicate(tmp_path):
    setup_project(tmp_path)
    source = Source(interrupt_at=2)
    partial, code = run(tmp_path, source)
    assert code == 2 and partial["stop_reason"] == "user_interrupt_checkpoint_saved"
    assert partial["metrics"]["requests"] == 1 and facts(tmp_path) == 2
    original_plan = Path(partial["plan_path"]).read_bytes()
    resumed_source = Source()
    complete, code = run(tmp_path, resumed_source, operation="resume", job_id=partial["job_id"])
    assert code == 0 and structural(complete) and len(resumed_source.calls) == 7
    assert facts(tmp_path) == 8 and Path(complete["plan_path"]).read_bytes() == original_plan
    replay_source = Source()
    replay, code = run(tmp_path, replay_source)
    assert replay["job_id"] == complete["job_id"] and code == 0
    assert replay_source.calls == [] and replay["metrics"]["requests"] == 0 and facts(tmp_path) == 8


def test_same_day_update_keeps_source_correction_versions_and_original_report(tmp_path):
    setup_project(tmp_path)
    first, _ = run(tmp_path)
    old_quality = Path(first["quality_path"]).read_bytes()
    correction = Source(close="10.5", stamp="2026-09-10T23:00:00+08:00")
    second, code = run(tmp_path, correction, operation="update")
    assert code == 0 and structural(second) and second["job_id"] != first["job_id"]
    assert second["metrics"]["updated"] == 8 and facts(tmp_path) == 16
    assert Path(first["quality_path"]).read_bytes() == old_quality
    with sqlite3.connect(tmp_path / "offline/market.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM f2_adjustment_windows").fetchone()[0] == 8


def test_scope_or_target_mismatch_rejected_before_source_requests(tmp_path):
    setup_project(tmp_path)
    first, _ = run(tmp_path)
    source = Source()
    config_path = tmp_path / "market.json"
    config = json.loads(config_path.read_text("utf-8"))
    config["scope"] = "all_a"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="scope"):
        run(tmp_path, source)
    with pytest.raises(ValueError, match="scope"):
        run(tmp_path, source, operation="resume", job_id=first["job_id"])
    assert source.calls == []


def test_injected_sources_cannot_enter_research(tmp_path):
    setup_project(tmp_path)
    source = Source()
    with pytest.raises(ValueError, match="injected"):
        run_market(project_root=tmp_path, config_path="market.json", target_date=DAY,
                   mode="research", client=source, calendar_resolver=calendar)
    assert source.calls == [] and not (tmp_path / "offline/market.sqlite3").exists()


def test_source_permission_rejection_stops_immediately_without_shrinking_denominator(tmp_path):
    setup_project(tmp_path)
    source = Source(permission=True)
    result, code = run(tmp_path, source)
    assert code == 2 and result["stop_reason"] == "source_access_or_rate_stop"
    assert len(source.calls) == 1 and result["denominator"] == 4
    assert result["tasks"]["failed"] == 1 and result["tasks"]["pending"] == 7
    assert result["totals"]["missing_target"] == 4


def test_incomplete_adjustment_never_becomes_complete_by_concatenating_other_task_rows(tmp_path):
    setup_project(tmp_path)
    class MissingAdjusted(Source):
        def query(self, operation, **params):
            result = super().query(operation, **params)
            if params["adjustment_mode"] == "forward_adjusted" and params["code"] == "sh.900003":
                result["rows"] = result["rows"][:1]
                result["raw_hash"] = raw_hash(result["fields"], result["rows"])
            return result
    result, code = run(tmp_path, MissingAdjusted())
    assert code == 2 and result["totals"]["valid_target"] == 4
    assert result["totals"]["adjustment_ready"] == 3
    assert result["tasks"]["failed"] == 1
    with sqlite3.connect(tmp_path / "offline/market.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM f2_adjustment_windows").fetchone()[0] == 3


def test_frozen_plan_hash_corruption_blocks_resume_and_quality(tmp_path):
    setup_project(tmp_path)
    result, _ = run(tmp_path, Source(interrupt_at=1))
    with sqlite3.connect(tmp_path / "offline/jobs.sqlite3") as connection:
        connection.execute("DROP TRIGGER f2_job_no_update")
        connection.execute("UPDATE f2_jobs SET plan_json='{}'")
    source = Source()
    with pytest.raises(ValueError, match="hash"):
        run(tmp_path, source, job_id=result["job_id"], operation="resume")
    with pytest.raises(ValueError, match="hash"):
        quality_report(tmp_path, "market.json", result["job_id"], mode="offline_test")
    assert source.calls == []


def test_task_request_corruption_cannot_change_stock_under_a_frozen_security_id(tmp_path):
    setup_project(tmp_path)
    result, _ = run(tmp_path, Source(interrupt_at=1))
    with sqlite3.connect(tmp_path / "offline/jobs.sqlite3") as connection:
        task_id, text = connection.execute("SELECT task_id,request_json FROM f2_tasks ORDER BY position LIMIT 1").fetchone()
        request = json.loads(text)
        request["symbol"] = "sh.600000"
        connection.execute("UPDATE f2_tasks SET request_json=? WHERE task_id=?", (json.dumps(request), task_id))
    source = Source()
    with pytest.raises(ValueError, match="task|request|checkpoint"):
        run(tmp_path, source, job_id=result["job_id"], operation="resume")
    assert source.calls == [] and facts(tmp_path) == 0


def test_calendar_failure_has_no_history_requests_and_preserves_blocked_evidence(tmp_path):
    setup_project(tmp_path)
    source = Source()
    result, code = run_market(project_root=tmp_path, config_path="market.json", target_date=DAY,
        mode="offline_test", client=source,
        calendar_resolver=lambda *args, **kwargs: {"verified": False, "resolved_trade_date": None, "error": "synthetic 10002007"})
    assert code == 2 and result["status"] == "f2_blocked" and source.calls == []
    assert (Path(result["output_directory"]) / "result.json").is_file()


def directory_hashes(path):
    source = _io(path)
    return {item.relative_to(source).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in source.rglob("*") if item.is_file()}


def test_quality_is_read_only_and_uses_frozen_job_versions_after_newer_correction(tmp_path):
    setup_project(tmp_path)
    first, _ = run(tmp_path)
    frozen = quality_report(tmp_path, "market.json", first["job_id"], mode="offline_test")
    # A new job corrects the trading status to suspended. Old job stays at its own facts.
    second, _ = run(tmp_path, Source(suspend_code="sh.900003", stamp="2026-09-10T23:00:00+08:00"), operation="update")
    assert second["totals"]["confirmed_suspended"] == 1
    before = directory_hashes(tmp_path)
    replayed = quality_report(tmp_path, "market.json", first["job_id"], mode="offline_test")
    assert replayed["totals"] == frozen["totals"]
    assert replayed["totals"]["confirmed_suspended"] == 0
    assert directory_hashes(tmp_path) == before


def test_quality_accepts_compact_and_legacy_record_checkpoints_without_mixing_versions(tmp_path):
    setup_project(tmp_path)
    result, _ = run(tmp_path)
    before = quality_report(tmp_path, "market.json", result["job_id"], mode="offline_test")
    with sqlite3.connect(tmp_path / "offline/jobs.sqlite3") as jobs:
        tasks = jobs.execute("SELECT task_id,request_json,result_json FROM f2_tasks ORDER BY position").fetchall()
        task_id, request_json, result_json = tasks[0]
        request, saved_result = json.loads(request_json), json.loads(result_json)
        assert "records" not in saved_result["saved"]["quality"]
        versions = saved_result["saved"].pop("raw_versions")
        assert len(versions) == 2
        with sqlite3.connect(tmp_path / "offline/market.sqlite3") as market:
            records = [json.loads(market.execute(
                "SELECT payload_json FROM f2_bar_versions WHERE security_id=? AND trade_date=? AND fact_hash=?",
                (request["security_id"], day, fact_hash)).fetchone()[0]) for day, fact_hash in versions.items()]
        saved_result["saved"]["quality"]["records"] = records
        jobs.execute("UPDATE f2_tasks SET result_json=? WHERE task_id=?", (json.dumps(saved_result), task_id))
    after = quality_report(tmp_path, "market.json", result["job_id"], mode="offline_test")
    assert after["totals"] == before["totals"] and structural(after)


def copied_checkpoint(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    setup_project(source_root)
    config_path = source_root / "market.json"
    config = json.loads(config_path.read_text("utf-8"))
    # This is an allowed backup-reference scope without any research path component.
    config["output_directory"] = "outputs/verification/offline-f2"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    partial, _ = run(source_root, Source(interrupt_at=2))
    restored = tmp_path / "restored"
    shutil.copytree(_io(source_root), _io(restored))
    return source_root, restored, partial


def test_cross_root_resume_without_verified_restore_map_cannot_write_old_workspace(tmp_path):
    original, restored, partial = copied_checkpoint(tmp_path)
    before = directory_hashes(original)
    source = Source()
    with pytest.raises(ValueError, match="映射|restore"):
        run(restored, source, operation="resume", job_id=partial["job_id"])
    assert source.calls == [] and directory_hashes(original) == before


def test_verified_restore_map_routes_resume_to_new_root_preserving_frozen_plan(tmp_path):
    original, restored, partial = copied_checkpoint(tmp_path)
    relative = Path(partial["plan_path"]).relative_to(original).as_posix()
    body = (restored / relative).read_bytes()
    manifest = {"schema_version": "m4-backup-v1", "source_project_root": str(original),
                "files": [{"path": relative, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "kind": "immutable_file"}]}
    mapping = {"schema_version": "m4-restore-map-v1", "source_project_root": str(original),
               "restored_project_root": str(restored), "paths": [relative]}
    (restored / "backup-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (restored / "restore-path-map.json").write_text(json.dumps(mapping), encoding="utf-8")
    before = directory_hashes(original)
    source = Source()
    result, code = run(restored, source, operation="resume", job_id=partial["job_id"])
    assert code == 0 and structural(result) and len(source.calls) == 7
    assert Path(result["output_directory"]).is_relative_to(restored)
    assert Path(result["plan_path"]).read_bytes() == body
    assert directory_hashes(original) == before
    report = quality_report(restored, "market.json", result["job_id"], mode="offline_test")
    assert structural(report)
