"""Offline safeguards: real OS locks / SQLite / file copies, no market or model HTTP."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

from ashare_daily.operations.backup import create_backup, restore_backup, verify_backup, resolve_restored_reference
from ashare_daily.operations.budget import BudgetExceeded, BudgetLedger
from ashare_daily.operations.lock import AlreadyRunning, ProcessLock


def child(code, *arguments):
    return subprocess.run([sys.executable, "-c", code, *map(str, arguments)], capture_output=True, text=True, timeout=30)


def test_real_process_lock_rejects_other_process_and_preserves_owner(tmp_path):
    path = tmp_path / "daily.lock"
    with ProcessLock(path, "owner"):
        result = child("""
from pathlib import Path
import sys
from ashare_daily.operations.lock import ProcessLock, AlreadyRunning
try:
    with ProcessLock(Path(sys.argv[1]), 'competitor'): pass
except AlreadyRunning as exc:
    print(exc.metadata['run_id'])
    raise SystemExit(9)
""", path)
        assert result.returncode == 9, result.stderr
        assert result.stdout.strip() == "owner"
        with pytest.raises(AlreadyRunning):
            ProcessLock(path, "same-process").acquire()
    with ProcessLock(path, "later") as acquired:
        assert not acquired.recovered_stale


def test_abrupt_process_exit_releases_os_lock_and_records_stale_metadata(tmp_path):
    path = tmp_path / "daily.lock"
    result = child("""
import os,sys
from pathlib import Path
from ashare_daily.operations.lock import ProcessLock
lock = ProcessLock(Path(sys.argv[1]), 'crashed').acquire()
os._exit(6)
""", path)
    assert result.returncode == 6
    with ProcessLock(path, "recovery") as acquired:
        assert acquired.recovered_stale
        assert acquired.previous_metadata["run_id"] == "crashed"


def test_lock_released_on_exception_and_bad_metadata_is_recoverable(tmp_path):
    path = tmp_path / "lock"
    path.write_bytes(b"\0partial-json")
    with pytest.raises(RuntimeError, match="test"):
        with ProcessLock(path, "first"):
            raise RuntimeError("test")
    with ProcessLock(path, "next") as current:
        assert current.metadata["pid"] == os.getpid()


def test_budget_persists_across_restart_run_ids_and_crash(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    ledger = BudgetLedger(path)
    reservation = ledger.reserve("2026-09-09", "run-1", 2)
    ledger.record_usage(reservation, {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}, "ok")
    BudgetLedger(path).reserve("2026-09-09", "crashed-before-result", 2)
    with pytest.raises(BudgetExceeded):
        BudgetLedger(path).reserve("2026-09-09", "new-run-id", 2)
    summary = ledger.summary("2026-09-09")
    assert summary["reserved_attempts"] == 2
    assert summary["provider_reported_usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    assert not summary["usage_complete"]
    assert summary["status_counts"] == {"ok": 1, "reserved": 1}
    assert summary["currency_cost"] is None
    BudgetLedger(path).reserve("2026-09-10", "next-day", 2)


def test_budget_competing_processes_cannot_overspend(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    BudgetLedger(path)
    code = """
from pathlib import Path
import sys
from ashare_daily.operations.budget import BudgetLedger,BudgetExceeded
b=BudgetLedger(Path(sys.argv[1]))
count=0
for _ in range(5):
    try: b.reserve('2026-09-09',sys.argv[2],7); count+=1
    except BudgetExceeded: pass
print(count)
"""
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda number: child(code, path, number), range(4)))
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    assert sum(int(result.stdout) for result in results) == 7
    assert BudgetLedger(path).summary("2026-09-09")["reserved_attempts"] == 7


@pytest.mark.parametrize("status", ["timeout", "rate_limited", "refused", "authentication_error", "error"])
def test_failed_attempts_are_never_refunded(tmp_path, status):
    ledger = BudgetLedger(tmp_path / "budget.db")
    reservation = ledger.reserve("2026-09-09", "run", 1)
    ledger.record_usage(reservation, None, status)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("2026-09-09", "new", 1)
    assert ledger.summary("2026-09-09")["provider_reported_usage"] is None


def test_usage_update_does_not_count_same_response_twice(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db")
    reservation = ledger.reserve("2026-09-09", "run", 3)
    ledger.record_usage(reservation, {"total_tokens": 15, "secret": "do-not-store", "prompt_tokens": True}, "ok")
    ledger.record_usage(reservation, {"total_tokens": 15}, "ok")
    assert ledger.summary("2026-09-09")["provider_reported_usage"] == {"total_tokens": 15}
    with pytest.raises(ValueError):
        ledger.record_usage("not-existing", None, "error")
    with pytest.raises(ValueError):
        ledger.record_usage(reservation, None, "unknown secret error text")


def test_budget_uses_beijing_calendar_day(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db")
    ledger.reserve(datetime(2026, 9, 8, 16, 0, tzinfo=ZoneInfo("UTC")), "run", 1)
    assert ledger.summary("2026-09-09")["reserved_attempts"] == 1
    with pytest.raises(ValueError):
        ledger.reserve(datetime(2026, 9, 9), "run", 1)
    with pytest.raises(ValueError):
        ledger.reserve("20260909", "run", 1)


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_budget_rejects_invalid_limits(tmp_path, limit):
    with pytest.raises(ValueError):
        BudgetLedger(tmp_path / "budget.db").reserve("2026-09-09", "run", limit)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    database = root / "data/research/market.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE sample (symbol TEXT, amount_yuan REAL)")
        db.execute("INSERT INTO sample VALUES ('sh.600000',50000000)")
    raw = root / "outputs/verification/m3/raw/body.raw"
    raw.parent.mkdir(parents=True)
    raw.write_text("OFFLINE TEST: 原始资料", encoding="utf-8")
    report = root / "outputs/research/m3/version/report.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"raw_locator": str(raw), "immutable": True}), encoding="utf-8")
    config = root / "config/model.json"
    config.parent.mkdir()
    config.write_text('{"model_name":"unit-test","key_present":true}', encoding="utf-8")
    (root / ".env").write_text("MODEL_API_KEY=sk-do-not-read-or-copy", encoding="utf-8")
    (root / "outputs/research/m3/private.log").write_text("sk-do-not-read-or-copy")
    ledger = BudgetLedger(root / "data/operations/runtime.sqlite3")
    ledger.reserve("2026-09-09", "attempt", 2)
    return root


def test_backup_restore_real_sqlite_and_immutable_references(project, tmp_path):
    report = project / "outputs/research/m3/version/report.json"
    original = report.read_bytes()
    result = create_backup(project, tmp_path / "backup")
    assert result["status"] == "ok"
    manifest = verify_backup(tmp_path / "backup")
    paths = [item["path"] for item in manifest["files"]]
    assert "outputs/verification/m3/raw/body.raw" in paths
    assert all(".env" not in path and ".log" not in path and ".lock" not in path for path in paths)
    restored = restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert restored["sqlite_integrity"] == "ok"
    assert restored["budget_ledger_restored"]
    mapped = resolve_restored_reference(str(report), tmp_path / "restored")
    assert mapped.read_bytes() == original == report.read_bytes()
    with sqlite3.connect(tmp_path / "restored/data/research/market.sqlite3") as db:
        assert db.execute("SELECT * FROM sample").fetchone() == ("sh.600000", 50000000.0)
    assert BudgetLedger(tmp_path / "restored/data/operations/runtime.sqlite3").summary("2026-09-09")["reserved_attempts"] == 1
    with pytest.raises(ValueError):
        resolve_restored_reference(str(project / ".env"), tmp_path / "restored")


def test_sqlite_backup_includes_committed_wal_transactions(project, tmp_path):
    connection = sqlite3.connect(project / "data/research/market.sqlite3")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO sample VALUES ('sz.000001',60000000)")
        connection.commit()
        create_backup(project, tmp_path / "backup")
        with sqlite3.connect(tmp_path / "backup/files/data/research/market.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM sample").fetchone()[0] == 2
        assert not list((tmp_path / "backup").rglob("*-wal"))
    finally:
        connection.close()


def test_restore_never_overwrites_original_or_existing_target(project, tmp_path):
    create_backup(project, tmp_path / "backup")
    with pytest.raises(ValueError, match="覆盖"):
        restore_backup(tmp_path / "backup", project)
    with pytest.raises(ValueError):
        create_backup(project, tmp_path / "backup")
    with pytest.raises(ValueError):
        create_backup(project, project / "outputs/research/backup")


@pytest.mark.parametrize("bad_path", ["../escape.txt", "/absolute.txt", "C:/escape.txt", "x/../escape", "x\\escape", ".env",
                                     "config/NUL", "config/test.", "config/test ", "unexpected/file.txt"])
def test_backup_manifest_path_attack_rejected(project, tmp_path, bad_path):
    create_backup(project, tmp_path / "backup")
    path = tmp_path / "backup/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = bad_path
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_backup_tamper_stops_restore_before_new_directory(project, tmp_path):
    create_backup(project, tmp_path / "backup")
    (tmp_path / "backup/files/config/model.json").write_text("tampered")
    with pytest.raises(ValueError, match="SHA256"):
        restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_config_secrets_refused_without_printing_value(project, tmp_path):
    (project / "config/model.json").write_text('{"api_key":"THIS-IS-SECRET"}')
    with pytest.raises(ValueError) as caught:
        create_backup(project, tmp_path / "backup")
    assert "THIS-IS-SECRET" not in str(caught.value)
    assert not (tmp_path / "backup/manifest.json").exists()


def test_backup_missing_referenced_evidence_is_a_gap(project, tmp_path):
    (project / "outputs/verification/m3/raw/body.raw").unlink()
    result = create_backup(project, tmp_path / "backup")
    assert result["status"] == "partial"
    assert result["missing_referenced_files"] == ["outputs/verification/m3/raw/body.raw"]


def test_windows_long_nested_report_paths_restore_without_registry_changes(project, tmp_path):
    relative = Path("outputs/research/m4/runs") / ("a" * 75) / ("b" * 75) / ("c" * 75) / ("d" * 75) / "body.raw"
    path = project / relative
    def accessible(value):
        return Path("\\\\?\\" + str(value.absolute())) if os.name == "nt" else value
    accessible(path.parent).mkdir(parents=True)
    accessible(path).write_bytes(b"OFFLINE long path content")
    assert len(str(path)) > 260
    backup = tmp_path / "long-backup"
    restore = tmp_path / "long-restored"
    create_backup(project, backup)
    result = restore_backup(backup, restore)
    assert result["status"] == "ok"
    assert accessible(restore / relative).read_bytes() == b"OFFLINE long path content"


def test_symlink_escape_rejected_when_platform_allows(project, tmp_path):
    link = project / "config/outside.json"
    outside = tmp_path / "outside.json"
    outside.write_text("outside")
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前账号或文件系统不允许创建符号链接；其他路径穿越检查仍运行")
    with pytest.raises(ValueError, match="符号链接"):
        create_backup(project, tmp_path / "backup")
