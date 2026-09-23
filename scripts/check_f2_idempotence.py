"""Replay one real archived raw response under the normal lock; never fetch data."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from ashare_daily.market_foundation import F2MarketStore
from ashare_daily.market_pipeline import JobStore, load_market_config
from ashare_daily.operations.backup import _io
from ashare_daily.operations.lock import ProcessLock

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--config", default="config/sse_szse_market.json")
    args = parser.parse_args()
    config = load_market_config(ROOT, args.config)
    with ProcessLock(ROOT / "data/operations/daily.lock", "f2-idempotence-check"):
        jobs = JobStore(ROOT / config["checkpoint_database"], "research")
        plan = jobs.load(args.job)
        task = next(item for item in jobs.tasks(plan) if item["status"] == "done" and json.loads(item["request_json"])["adjustment_mode"] == "unadjusted")
        request, saved = json.loads(task["request_json"]), json.loads(task["result_json"])
        response_path = Path(saved["response_path"])
        response_bytes = _io(response_path).read_bytes()
        response = json.loads(response_bytes)
        store = F2MarketStore(ROOT / config["database"])

        def counts():
            with sqlite3.connect(store.path) as db:
                return {name: db.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
                        for name in ("f2_bar_versions", "f2_bar_current", "f2_batches", "f2_bar_observations", "f2_adjustment_windows")}

        before = counts()
        arguments = dict(security_id=request["security_id"], symbol=request["symbol"], scope=plan["scope"],
            universe_snapshot_id=plan["universe_snapshot_id"], response=response, source_response_path=response_path,
            source_response_hash=hashlib.sha256(response_bytes).hexdigest(), trading_dates=request["expected_dates"],
            provenance_mode="online", adjustment_mode="unadjusted")
        first = store.save_batch(**arguments)
        revalidated = counts()
        result = store.save_batch(**arguments)
        after = counts()
    evidence = {"verification_kind": "real_archived_response_replay", "network_requests": 0,
                "job_id": args.job, "symbol": request["symbol"], "source_response_path": str(response_path),
                "before": before, "after_quality_revalidation": revalidated, "after_repeat": after,
                "passed": all(before[key] == revalidated[key] for key in ("f2_bar_versions", "f2_bar_current", "f2_adjustment_windows"))
                    and revalidated == after and first["inserted"] == first["updated"] == result["inserted"] == result["updated"] == 0,
                "quality_rules_version": result["quality"].get("quality_rules_version"),
                "note": "New quality rules may add an audit batch; repeated final rules must add neither facts nor audit batches.",
                "inserted": result["inserted"], "updated": result["updated"], "unchanged": result["unchanged"]}
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
