"""Exercise a restored real checkpoint without issuing another source request."""
import argparse
import hashlib
import json
from pathlib import Path

from ashare_daily.market_pipeline import quality_report, run_market
from ashare_daily.operations.backup import _io
from ashare_daily.operations.lock import ProcessLock

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with _io(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restored-root", type=Path, required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    restored = args.restored_root.resolve()
    original_files = [ROOT / "data/research/market.sqlite3", ROOT / "data/research/f2_jobs.sqlite3",
                      ROOT / "data/operations/runtime.sqlite3"]
    original_files += list((ROOT / "outputs/research/sse_szse_a/market/2026-09-11" / args.job).rglob("*.json"))
    before = {str(path): digest(path) for path in original_files}
    config = "config/sse_szse_market.json"
    quality = quality_report(restored, config, args.job)
    restored_plan = restored / "outputs/research/sse_szse_a/market/2026-09-11" / args.job / "plan.json"
    plan_hash = digest(restored_plan)
    with ProcessLock(restored / "data/operations/daily.lock", "f2-restored-checkpoint-check"):
        result, code = run_market(project_root=restored, config_path=config, job_id=args.job,
                                  operation="resume", max_seconds=0.000001)
    after = {str(path): digest(path) for path in original_files}
    passed = (before == after and result["metrics"]["requests"] == 0 and digest(restored_plan) == plan_hash
              and Path(result["quality_path"]).is_relative_to(restored)
              and quality["totals"] == result["totals"] and result["status"] == "f2_partial")
    print(json.dumps({"passed": passed, "verification_kind": "real_backup_local_restore_replay",
        "restored_root": str(restored), "source_requests": result["metrics"]["requests"],
        "original_files_unchanged": len(before) if before == after else False,
        "frozen_plan_sha256_preserved": digest(restored_plan) == plan_hash,
        "restore_resume_exit_code": code, "restore_resume_status": result["status"],
        "restored_quality_path": result["quality_path"], "totals": result["totals"],
        "note": "Deadline expires during checkpoint validation; real archived facts are read, no network acceptance is claimed."},
        ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
