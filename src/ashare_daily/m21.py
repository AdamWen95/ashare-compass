"""M2.1 uses the existing snapshots, formulas and report publishing conventions."""

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.m1_settings import SAMPLE_ID, SAMPLE_TYPES
from ashare_daily.m2 import _write
from ashare_daily.screening.engine import digest
from ashare_daily.screening.m21 import evaluate_m21, load_m21_config
from ashare_daily.screening.snapshots import freeze_input, load_snapshot, read_bundle, read_market_input


def run_m21(*, target_date: date | None = None, snapshot: str | None = None,
            config_path: Path = Path("config/m21_6.json"), database: Path = Path("data/research/market.sqlite3"),
            output_dir: Path = Path("outputs"), adjusted_dir: Path = Path("data/research/m2_adjusted"),
            fetch_adjusted: bool = False, refresh_adjusted: bool = False, adjusted_manifest: Path | None = None,
            eligibility_evidence: Path | None = None, project_root: Path | None = None) -> dict:
    from ashare_daily.reports.m21_render import render_m21_html, render_m21_markdown, write_m21_audit_csv
    from ashare_daily.eligibility import load_evidence_bundle, resolve_eligibility

    root = Path(output_dir).resolve() / "research" / "m21"
    if {part.lower() for part in root.parts} & {"demo", "fixtures"}:
        raise ValueError("M2.1 真实结果不能放入 DEMO/fixtures 目录")
    if (target_date is None) == (snapshot is None):
        raise ValueError("必须且只能指定 --date 或 --snapshot")
    if snapshot:
        if fetch_adjusted or refresh_adjusted or adjusted_manifest or eligibility_evidence:
            raise ValueError("重放快照不接受取数、刷新或新资格证据")
        frozen, snapshot_path = load_snapshot(snapshot, root)
        if frozen["strategy_config"].get("workflow_version") != "m2.1":
            raise ValueError("请使用 M2 流程重放旧 M2 快照；不要静默改写旧版本语义")
    else:
        config = load_m21_config(config_path)
        if config.sample_file:
            from ashare_daily.sample_data import load_sample_config
            selection = load_sample_config((project_root or Path()) / config.sample_file)
            sample_types = selection["symbol_types"]
        else:
            selection = {"sample_id": SAMPLE_ID, "selection_method": "沿用原 M1 六股与基准，未按表现重新挑选", "symbol_types": dict(SAMPLE_TYPES)}
            sample_types = dict(SAMPLE_TYPES)
        inputs = read_market_input(database, target_date, config, sample_types=sample_types)
        inputs["workflow_version"] = "m2.1"
        inputs["sample_selection"] = selection
        bundle = None
        adjusted_root = Path(adjusted_dir).resolve()
        if {part.lower() for part in adjusted_root.parts} & {"demo", "fixtures"}:
            raise ValueError("调整数据不能保存到 DEMO/fixtures")
        cache_key = digest({"sample_types": inputs["sample_types"], "trading_dates": inputs["trading_dates"]})
        cache_path = adjusted_root / f"window-{cache_key}.json"
        if adjusted_manifest and refresh_adjusted:
            raise ValueError("已有数据包不能与刷新同时指定")
        if adjusted_manifest:
            bundle = read_bundle(adjusted_manifest, anchor=adjusted_root)
        elif cache_path.is_file() and not refresh_adjusted:
            pointer = json.loads(cache_path.read_text(encoding="utf-8"))
            bundle = read_bundle(pointer["manifest_path"], anchor=cache_path)
            if bundle["manifest_hash"] != pointer["manifest_hash"]:
                raise ValueError("调整缓存指针哈希不匹配")
        if (fetch_adjusted and bundle is None or refresh_adjusted) and inputs["trading_dates"] and inputs["target_is_trading"] is True and not inputs["calendar_issues"]:
            if len(sample_types) <= 10:
                from ashare_daily.m2_data import prepare_adjusted_data
                bundle = prepare_adjusted_data(sample_types, [date.fromisoformat(day) for day in inputs["trading_dates"]], adjusted_root)
            else:
                raise ValueError("扩展样本请先运行 collect-sample，再通过 --adjusted-manifest 指定汇总数据包；不在日报内隐式批量抓取")
        evidence_path = eligibility_evidence or ((project_root or Path()) / config.eligibility_evidence_file if config.eligibility_evidence_file else None)
        evidence = load_evidence_bundle(evidence_path)
        if evidence.get("verification_kind") == "offline_test" and inputs["verification_kind"] != "offline_test":
            raise ValueError("离线测试资格证据不能用于真实行情报告")
        inputs["eligibility_evidence_bundle"] = evidence
        inputs["eligibility_imported_at"] = datetime.now(SHANGHAI).isoformat()
        stock_symbols = [symbol for symbol, kind in sample_types.items() if kind == "stock"]
        inputs["eligibility_states"] = resolve_eligibility(evidence, stock_symbols, target_date)
        frozen, snapshot_path = freeze_input(inputs, bundle, root)
        if bundle is not None and bundle.get("status") == "ok":
            adjusted_root.mkdir(parents=True, exist_ok=True)
            _write(cache_path, {"manifest_path": str(Path(bundle["run_directory"]) / "manifest.json"), "manifest_hash": bundle["manifest_hash"]}, exclusive=False)
    if frozen.get("verification_kind") == "offline_test":
        root = Path(output_dir).resolve() / "offline_test" / "m21"
    report = evaluate_m21(frozen)
    generated = datetime.now(SHANGHAI)
    report.update(actual_generated_at=generated.isoformat(), snapshot_path=str(snapshot_path),
                  input_frozen_at=frozen["frozen_at"], adjustment_batch_id=frozen.get("adjusted_data", {}).get("batch_id"),
                  adjustment_manifest_hash=frozen.get("adjusted_data", {}).get("manifest_hash"))
    directory = root / report["trade_date"] / (generated.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    _write(directory / "daily_brief.json", report)
    for filename, content in (("daily_brief.md", render_m21_markdown(report)), ("daily_brief.html", render_m21_html(report))):
        with (directory / filename).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    count = write_m21_audit_csv(report, directory / "screening_audit.csv")
    _write(directory / "manifest.json", {"schema_version": "m21-report-manifest-v1", "snapshot_id": report["snapshot_id"],
        "snapshot_path": str(snapshot_path), "strategy_version": report["strategy_version"],
        "status_semantics_version": report["status_semantics_version"], "config_hash": report["config_hash"], "result_hash": report["result_hash"],
        "files": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in ("daily_brief.json", "daily_brief.md", "daily_brief.html", "screening_audit.csv")}})
    result = {"workflow_version": "m2.1", "generation_status": "ok", "status": report["status"], "verification_kind": report["verification_kind"],
              "notice": report["notice"], "trade_date": report["trade_date"], "actual_market_date": report["actual_market_date"],
              "counts": report["counts"], "count_definitions": report["count_definitions"], "gaps": report["gaps"],
              "snapshot_id": report["snapshot_id"], "snapshot_path": str(snapshot_path), "result_hash": report["result_hash"],
              "csv_record_count": count, "run_directory": str(directory), "html": str(directory / "daily_brief.html"),
              "markdown": str(directory / "daily_brief.md"), "json": str(directory / "daily_brief.json"), "csv": str(directory / "screening_audit.csv")}
    _write(directory / "result.json", result)
    _write(root / "latest.json", result, exclusive=False)
    return result
