"""Manual F3-S dispatch and immutable technical observations, no publication."""
from copy import deepcopy
import csv
from datetime import datetime
import hashlib
import io
import json
import re
from pathlib import Path

from .market_schemas import SHANGHAI
from .operations.daily import local_path, atomic_json
from .sector_pipeline import load_config, read_selection, write_new
from .sector_selection import digest

from .sector_validation import freeze_validation, read_validation


def _selection(root, selection_id, validation_config_path):
    if selection_id.startswith("validation-sector-"):
        return read_validation(root, selection_id, config_path=validation_config_path)
    config = load_config(root, "config/sector_first.json")
    return read_selection(root, config, selection_id)


def _output(root, selection, config):
    if selection.get("purpose") == "engineering_validation":
        return local_path(root, config["output_directory"]) / selection["selection_id"] / "screening"
    return root / "outputs/research/sse_szse_a/sector_first_screening" / selection["selection_id"]


def _read_observation(directory, expected_selection_id, selection=None):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "f3s-technical-manifest-v1" or manifest.get("selection_id") != expected_selection_id:
        raise ValueError("technical_manifest_selection_mismatch")
    required = {"technical_observation.md", "technical_evaluation.json", "technical_evaluation.csv", "screening_inputs.json", "strategy_config.json"}
    if set(manifest.get("files", {})) != required or manifest.get("evaluation_id") != directory.name:
        raise ValueError("technical_manifest_file_contract_incomplete")
    for name, checksum in manifest["files"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != checksum:
            raise ValueError("technical_observation_hash_mismatch")
    result = json.loads((directory / "technical_evaluation.json").read_text(encoding="utf-8"))
    from .sector_screening import screening_input_hash
    inputs = json.loads((directory / "screening_inputs.json").read_text(encoding="utf-8"))
    expected_purpose = "engineering_validation" if expected_selection_id.startswith("validation-sector-") else "production"
    if (inputs.get("input_hash") != screening_input_hash(inputs) or inputs.get("input_hash") != manifest.get("input_hash")
            or result.get("input_hash") != inputs.get("input_hash") or inputs.get("selection_id") != expected_selection_id
            or inputs.get("purpose") != expected_purpose or manifest.get("purpose") != expected_purpose
            or manifest.get("production_eligible") is not (expected_purpose == "production")):
        raise ValueError("technical_input_purpose_or_binding_mismatch")
    if selection is not None and (manifest.get("selection_content_hash") != selection["content_hash"]
            or inputs.get("selection_content_hash") != selection["content_hash"]):
        raise ValueError("technical_input_selection_hash_mismatch")
    if result.get("result_hash") != digest({key: value for key, value in result.items() if key != "result_hash"}):
        raise ValueError("technical_result_content_hash_mismatch")
    if (result.get("selection_id") != expected_selection_id or result.get("purpose") != manifest.get("purpose")
            or result.get("production_eligible") != manifest.get("production_eligible")
            or result.get("input_fingerprint") != manifest.get("data_fingerprint")):
        raise ValueError("technical_observation_purpose_conflict")
    return result, manifest


def _summary(result, directory, *, reused=False):
    return {"status": result.get("status", "technical_observation_complete"), "selection_id": result["selection_id"],
        "purpose": result["purpose"], "production_eligible": result["production_eligible"],
        "counts": result.get("counts", {}), "evaluation_id": directory.name, "reused_evaluation": reused,
        "json_path": str(directory / "technical_evaluation.json"), "csv_path": str(directory / "technical_evaluation.csv"),
        "report_path": str(directory / "technical_observation.md"), "model_calls": 0, "network_requests": 0}


def _markdown(result, selection):
    validation = selection.get("purpose") == "engineering_validation"
    title = "真实数据工程验收·技术观察（不代表自动入选或投资关注）" if validation else "沪深 A 股·板块精选技术观察"
    lines = ["# " + title, "", "暂不含北交所；未进行公告与公司证据研究，不是已核验投资机会。", "",
        f"冻结编号：{selection['selection_id']}；行情目标日：{selection['target_date']}",
        f"原资料截点：{selection.get('source_cutoff_at', selection['cutoff_at'])}；本范围冻结截点：{selection['cutoff_at']}",
        "时间口径：当日观察及次日补证重建；不把后来取得的数据倒填为原截点前已知。", "",
        "本报告仅使用原有量价规则，不是未来收益预测；技术与资格为两条独立状态轴。", "",
        "| 统计 | 数值 |", "|---|---:|"]
    for key, value in result.get("counts", {}).items():
        labels = {"stock_count": "完整冻结集合", "technical_pass_count": "技术通过", "technical_fail_count": "技术未通过",
            "technical_unknown_count": "技术无法完整计算", "technical_not_applicable_count": "技术不适用",
            "eligibility_pass_count": "资格通过", "eligibility_fail_count": "资格未通过", "eligibility_pending_count": "资格待核查",
            "candidate_count": "用途允许的量价预候选", "formal_verified_opportunity_count": "已核验投资机会",
            "technical_pass_eligibility_pending_count": "技术通过但资格待核查"}
        lines.append(f"| {labels.get(key, key)} | {'N/A' if value is None else value} |")
    if not selection["members"]:
        lines += ["", "生产集合S=0：历史和筛选未触发/不适用，比例N/A；没有调用工程集合补足。"]
    lines += ["", "## 全部成员技术与资格", "", "| 证券 | 名称 | 上市板块 | 行业 | 技术 | 资格 | 风险和缺口 |", "|---|---|---|---|---|---|---|"]
    status_zh = {"pass": "通过", "fail": "未通过", "unknown": "无法完整计算", "pending": "待核查", "not_applicable": "不适用"}
    names = {m["security_id"]: m.get("name", "") for m in selection["members"]}
    for row in result.get("evaluations", []):
        issues = row.get("risk_gaps", []) + row.get("data_issues", [])
        columns = [row.get("symbol", row["security_id"]), names.get(row["security_id"], ""), row.get("listing_board", row.get("board", "")),
            ",".join(row.get("sector_ids", [])), status_zh.get(row.get("technical_status"), row.get("technical_status", "待核验")),
            status_zh.get(row.get("eligibility_status"), row.get("eligibility_status", "待核验")), "; ".join(map(str, issues))]
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in columns) + " |")
    for row in result.get("evaluations", []):
        lines += ["", "## " + str(row.get("symbol", row["security_id"])), "",
            "技术通过但资格待核查时，只作待核查技术观察，不能作为已核验候选。", "",
            "| 指标 | 数值 |", "|---|---:|"]
        metric_names = {"adjusted_close": "前复权收盘（元/股）", "ma20": "MA20（元/股）", "ma60": "MA60（元/股）",
            "stock_return_20": "股票20日收益（比值）", "benchmark_return_20": "上证综指20日收益（比值）",
            "relative_return_20": "超额收益（比值）", "avg_amount_20_cny": "20日均成交额（元）",
            "valid_history_count": "120日窗口有效行情日", "display_close": "未复权收盘（元/股）"}
        for key, value in row.get("metrics", {}).items():
            lines.append(f"| {metric_names.get(key, key)} | {'无法计算' if value is None else value} |")
        dates = row.get("metric_basis", {}).get("calculation_dates", [])
        lines += ["", f"计算交易日窗口：{dates[0] if dates else 'N/A'} 至 {dates[-1] if dates else 'N/A'}，共{len(dates)}日，含目标日。20日收益用末21点；停牌不缩短窗口。", "",
            "| 条件 | 状态 | 原因 |", "|---|---|---|"]
        for condition in row.get("technical_conditions", []) + row.get("eligibility_conditions", []):
            lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in
                (condition["label"], status_zh.get(condition["status"], condition["status"]), condition["reason"])) + " |")
        versions = row.get("source_versions", {})
        lines += ["", f"原价来源：{', '.join(versions.get('raw_providers', [])) or '待核验'}；复权来源：{versions.get('adjustment_provider') or '待核验'}；复权窗口：{versions.get('adjustment_window_id') or '未完整就绪'}。",
            "来源限制：" + "；".join(map(str, row.get("source_limitations", []))) if row.get("source_limitations") else "",
            "全部日期、事实哈希、锚点及精确输入版本保存在同目录JSON/CSV中。"]
    lines += ["", "320交易日是历史缓存目标；原120有效日、20日均成交额5000万元等要求未调整。",
        "工程验证结果禁止进入生产日报、今日候选、通知和生产统计。" if validation else "不覆盖关注行业以外的个股机会。",
        "模型调用0；未实施F4-S公告/公司研究、部署或自动调度。", ""]
    return "\n".join(lines)


def screen_frozen(root, selection_id, *, validation_config_path="config/sector_validation.json",
                  strategy_config_path="config/sector_screening.json", dry_run=False, report_only=False):
    from .sector_screening import validate_screening_config, screening_input_hash, evaluate_selection
    root = Path(root).resolve()
    selection, config = _selection(root, selection_id, validation_config_path)
    strategy = validate_screening_config(json.loads(local_path(root, strategy_config_path).read_text(encoding="utf-8")))
    base = _output(root, selection, config)
    if dry_run:
        return {"status": "dry_run", "selection_id": selection_id, "purpose": selection.get("purpose", "production"),
            "denominator": len(selection["members"]), "network_requests": 0, "model_calls": 0}, 0
    pointer_path = base / "latest_screening.json"
    previous = json.loads(pointer_path.read_text(encoding="utf-8")) if pointer_path.is_file() else None
    if previous and (previous.get("selection_id") != selection_id or not re.fullmatch(r"f3s-[a-f0-9]{24}", previous.get("evaluation_id", ""))):
        raise ValueError("technical_report_pointer_invalid")
    if report_only:
        if not previous:
            return {"status": "screening_not_run", "selection_id": selection_id, "model_calls": 0}, 2
        directory = base / previous["evaluation_id"]
        result, _ = _read_observation(directory, selection_id, selection)
        return _summary(result, directory, reused=True), 0
    if selection["members"]:
        from .sector_history import screening_history_inputs
        from .sector_benchmark import read_benchmark
        history = screening_history_inputs(root, selection, config)
        benchmark = read_benchmark(root, selection, config)
    else:
        history = {"calendar": {"verified": True, "trading_dates": [], "issues": []}, "securities": {}, "file_refs": [], "issues": []}
        benchmark = {"status": "not_applicable", "records": [], "issues": [], "file_refs": []}
    purpose = selection.get("purpose", "production")
    implementation = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in ("sector_workflow.py", "sector_screening.py", "sector_history.py", "factors/trend.py")}
    basis = {"selection_hash": selection["content_hash"], "history": history, "benchmark": benchmark,
        "strategy": strategy, "implementation_hashes": implementation}
    fingerprint = digest(basis)
    if previous and previous.get("data_fingerprint") == fingerprint:
        directory = base / previous["evaluation_id"]
        result, _ = _read_observation(directory, selection_id, selection)
        if result.get("input_fingerprint") != fingerprint:
            raise ValueError("technical_cache_fingerprint_mismatch")
        return _summary(result, directory, reused=True), 0
    inputs = {"schema_version": "f3s-screening-input-v1", "mode": selection["mode"], "purpose": purpose,
        "selection_id": selection_id, "selection_content_hash": selection["content_hash"], "target_date": selection["target_date"],
        "cutoff_at": datetime.now(SHANGHAI).isoformat(), "calendar": history["calendar"], "securities": history["securities"],
        "benchmark": benchmark, "file_refs": history.get("file_refs", []) + benchmark.get("file_refs", []),
        "historical_reconstruction": True, "source_cutoff_at": selection.get("source_cutoff_at", selection["cutoff_at"]),
        "implementation_hashes": implementation,
        "history_issues": history.get("issues", [])}
    inputs["input_hash"] = screening_input_hash(inputs)
    result = evaluate_selection(selection, inputs, strategy)
    result.update(input_fingerprint=fingerprint, title="真实数据工程验收·技术观察" if purpose == "engineering_validation" else "沪深 A 股·板块精选技术观察",
        selection_id=selection_id, purpose=purpose, production_eligible=purpose == "production", model_calls=0,
        formal_verified_opportunity_count=0)
    result["result_hash"] = digest({key: value for key, value in result.items() if key != "result_hash"})
    evaluation_id = "f3s-" + digest({"input": inputs["input_hash"], "strategy": strategy})[:24]
    directory = base / evaluation_id
    hashes = {"screening_inputs.json": write_new(directory / "screening_inputs.json", inputs),
        "strategy_config.json": write_new(directory / "strategy_config.json", strategy),
        "technical_evaluation.json": write_new(directory / "technical_evaluation.json", result)}
    stream = io.StringIO(newline="")
    fields = ["security_id", "symbol", "name", "exchange", "security_type", "listing_board", "sector_ids", "technical_status", "eligibility_status", "technical_rank",
              "purpose", "production_eligible", "metrics", "diagnostic_metrics", "metric_basis", "technical_conditions", "eligibility_conditions", "risk_gaps",
              "source_versions", "source_limitations", "data_issues", "raw_facts_ready", "adjustment_ready", "latest_raw_date"]
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in result.get("evaluations", []):
        writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False, sort_keys=True) if isinstance(row.get(key), (dict, list)) else row.get(key) for key in fields})
    for name, body in (("technical_evaluation.csv", stream.getvalue().encode("utf-8-sig")),
                       ("technical_observation.md", _markdown(result, selection).encode())):
        with (directory / name).open("xb") as output:
            output.write(body)
        hashes[name] = hashlib.sha256(body).hexdigest()
    write_new(directory / "manifest.json", {"schema_version": "f3s-technical-manifest-v1", "selection_id": selection_id,
        "evaluation_id": evaluation_id, "purpose": purpose, "production_eligible": purpose == "production",
        "formal_verified_opportunity_count": 0, "data_fingerprint": fingerprint,
        "selection_content_hash": selection["content_hash"], "input_hash": inputs["input_hash"], "files": hashes})
    atomic_json(pointer_path, {"selection_id": selection_id, "evaluation_id": evaluation_id, "data_fingerprint": fingerprint})
    return _summary(result, directory), 0


def run_f3s(root, args):
    root = Path(root).resolve()
    operation = args.sector_command
    if operation == "validation-freeze":
        return freeze_validation(root, args.source_selection, config_path=args.validation_config, dry_run=args.dry_run), 0
    if operation.startswith("validation-"):
        selection, config = read_validation(root, args.selection, config_path=args.validation_config,
            require_permission=not args.dry_run and operation in {"validation-prepare", "validation-resume"})
        if operation == "validation-benchmark":
            if args.dry_run:
                return {"status": "dry_run", "selection_id": selection["selection_id"], "purpose": "engineering_validation",
                    "production_eligible": False, "benchmark_id": "sh.000001", "network_requests": 0,
                    "database_writes": 0, "model_calls": 0}, 0
            from .sector_benchmark import prepare_benchmark
            result = prepare_benchmark(root, selection, config, online=args.online and not args.dry_run,
                                       max_seconds=min(args.max_seconds, 30))
            return {key: result.get(key) for key in ("status", "selection_id", "purpose", "production_eligible", "model_calls", "path", "issues", "metrics")}, 0 if result.get("verified") else 2
        from .sector_history import prepare_history
        result = prepare_history(root, selection, config, dry_run=args.dry_run, max_seconds=args.max_seconds)
        summary = {key: result.get(key) for key in ("status", "selection_id", "target_date", "denominator", "history_ready_count",
            "adjustment_ready_count", "risk_unknown_count", "pending_count", "metrics", "report_path", "stop_reason", "model_calls")}
        summary.update(purpose="engineering_validation", production_eligible=False)
        return summary, 0 if args.dry_run or result.get("pending_count") == 0 else 2
    return screen_frozen(root, args.selection, validation_config_path=args.validation_config,
                         strategy_config_path=args.strategy_config, dry_run=args.dry_run,
                         report_only=operation == "technical-report")
