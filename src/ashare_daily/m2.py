"""M2 CLI orchestration: immutable input -> pure screening -> fixed reports."""

from datetime import date, datetime
import json
from pathlib import Path
from uuid import uuid4

from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.screening.engine import digest, evaluate_snapshot
from ashare_daily.screening.settings import load_config
from ashare_daily.screening.snapshots import freeze_input, load_snapshot, read_bundle, read_market_input


def _write(path: Path, value, *, exclusive=True):
    with path.open("x" if exclusive else "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def run_m2(*, target_date: date | None = None, snapshot: str | None = None,
           config_path: Path = Path("config/m2.json"), database: Path = Path("data/research/market.sqlite3"),
           output_dir: Path = Path("outputs"), adjusted_dir: Path = Path("data/research/m2_adjusted"),
           fetch_adjusted: bool = False, refresh_adjusted: bool = False, adjusted_manifest: Path | None = None) -> dict:
    from ashare_daily.reports.m2_render import render_m2_html, render_m2_markdown, write_audit_csv

    root = Path(output_dir).resolve() / "research" / "m2"
    if {part.lower() for part in root.parts} & {"demo", "fixtures"}:
        raise ValueError("M2 真实结果不能放入 DEMO/fixtures 目录")
    if (target_date is None) == (snapshot is None):
        raise ValueError("必须且只能指定 --date 或 --snapshot")
    if snapshot:
        if fetch_adjusted or refresh_adjusted or adjusted_manifest:
            raise ValueError("重放快照不接受取数或刷新参数")
        frozen, snapshot_path = load_snapshot(snapshot, root)
    else:
        config = load_config(config_path)
        inputs = read_market_input(database, target_date, config)
        bundle = None
        adjusted_root = Path(adjusted_dir).resolve()
        if {part.lower() for part in adjusted_root.parts} & {"demo", "fixtures"}:
            raise ValueError("调整数据不能保存到 DEMO/fixtures")
        cache_key = digest({"sample_types": inputs["sample_types"], "trading_dates": inputs["trading_dates"]})
        cache_path = adjusted_root / f"window-{cache_key}.json"
        if adjusted_manifest and refresh_adjusted:
            raise ValueError("显式已有数据包不能与刷新同时指定")
        if adjusted_manifest:
            bundle = read_bundle(adjusted_manifest, anchor=adjusted_root)
        elif cache_path.is_file() and not refresh_adjusted:
            pointer = json.loads(cache_path.read_text(encoding="utf-8"))
            bundle = read_bundle(pointer["manifest_path"], anchor=cache_path)
            if bundle["manifest_hash"] != pointer["manifest_hash"]:
                raise ValueError("调整数据缓存指针哈希不匹配")
        if (fetch_adjusted and bundle is None or refresh_adjusted) and inputs["trading_dates"] and inputs["target_is_trading"] is True and not inputs["calendar_issues"]:
            from ashare_daily.m2_data import prepare_adjusted_data
            bundle = prepare_adjusted_data(inputs["sample_types"], [date.fromisoformat(day) for day in inputs["trading_dates"]], adjusted_root)
        frozen, snapshot_path = freeze_input(inputs, bundle, root)
        if bundle is not None and bundle.get("status") == "ok":
            adjusted_root.mkdir(parents=True, exist_ok=True)
            _write(cache_path, {"manifest_path": str(Path(bundle["run_directory"]) / "manifest.json"), "manifest_hash": bundle["manifest_hash"]}, exclusive=False)
    if frozen.get("verification_kind") == "offline_test":
        root = Path(output_dir).resolve() / "offline_test" / "m2"
    report = evaluate_snapshot(frozen)
    generated = datetime.now(SHANGHAI)
    report["actual_generated_at"] = generated.isoformat()
    report["snapshot_path"] = str(snapshot_path)
    report["input_frozen_at"] = frozen["frozen_at"]
    report["adjustment_batch_id"] = frozen.get("adjusted_data", {}).get("batch_id")
    report["adjustment_manifest_hash"] = frozen.get("adjusted_data", {}).get("manifest_hash")
    run_dir = root / report["trade_date"] / (generated.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=False)
    _write(run_dir / "daily_brief.json", report)
    for filename, content in (("daily_brief.md", render_m2_markdown(report)), ("daily_brief.html", render_m2_html(report))):
        with (run_dir / filename).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    csv_count = write_audit_csv(report, run_dir / "screening_audit.csv")
    manifest = {"schema_version": "m2-report-manifest-v1", "snapshot_id": report["snapshot_id"],
                "snapshot_path": str(snapshot_path), "strategy_version": report["strategy_version"],
                "config_hash": report["config_hash"], "result_hash": report["result_hash"], "files": {}}
    import hashlib
    for filename in ("daily_brief.json", "daily_brief.md", "daily_brief.html", "screening_audit.csv"):
        manifest["files"][filename] = hashlib.sha256((run_dir / filename).read_bytes()).hexdigest()
    _write(run_dir / "manifest.json", manifest)
    result = {"status": report["status"], "generation_status": "ok", "verification_kind": report["verification_kind"],
              "notice": report["notice"], "trade_date": report["trade_date"], "actual_market_date": report["actual_market_date"],
              "counts": report["counts"], "gaps": report["gaps"], "snapshot_id": report["snapshot_id"],
              "snapshot_path": str(snapshot_path), "result_hash": report["result_hash"], "csv_record_count": csv_count,
              "run_directory": str(run_dir), "html": str(run_dir / "daily_brief.html"),
              "markdown": str(run_dir / "daily_brief.md"), "json": str(run_dir / "daily_brief.json"),
              "csv": str(run_dir / "screening_audit.csv")}
    _write(run_dir / "result.json", result)
    _write(root / "latest.json", result, exclusive=False)
    return result
