"""Manual, revisioned F4-S1 closure; no publication, model or scheduler entry."""
from copy import deepcopy
import csv
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import re

from .market_schemas import SHANGHAI
from .operations.daily import local_path
from .operations.paths import resolve_archived_path
from .sector_pipeline import write_new
from .sector_selection import digest
from .sector_workflow import _selection


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ref(root, value):
    path = Path(value)
    path = resolve_archived_path(path if path.is_absolute() else root / path, anchor=root)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _company_basis(root, selection):
    if not selection["members"]:
        return None
    from .sector_company_evidence import _index
    snapshot, _, _ = _index(root, selection["mode"])
    return {"index_hash": snapshot["content_hash"], "registry_sha256": _ref(root, "config/m3_sources.json")["sha256"],
        "window_config_sha256": _ref(root, "config/sector_first_daily.json")["sha256"]}


def _base(root, selection, config):
    # F4-S1 has no production nonempty or publishing entry. Empty production
    # observations have a separate directory and cannot use engineering data.
    if selection.get("purpose") == "engineering_validation":
        return local_path(root, config["output_directory"]) / selection["selection_id"] / "f4s1/revisions"
    return root / "outputs/research/sse_szse_a/sector_first_f4s1" / selection["selection_id"]


def _bytes(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError("immutable_f4s1_artifact_conflict")
    else:
        with path.open("xb") as stream:
            stream.write(body)
    return hashlib.sha256(body).hexdigest()


def _csv(path, rows, fields=None):
    fields = fields or sorted({key for row in rows for key in row}) or ["security_id", "status"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False, sort_keys=True) if isinstance(row.get(key), (dict, list)) else row.get(key) for key in fields})
    return _bytes(path, stream.getvalue().encode("utf-8-sig"))


def _seal(value):
    return {**value, "content_hash": digest(value)}


def _read_sealed(path):
    value = _json(path)
    if value.get("content_hash") != digest({key: item for key, item in value.items() if key != "content_hash"}):
        raise ValueError("f4s1_content_hash_mismatch")
    return value


def read_revision(directory, selection, *, root=None):
    root = Path(root or Path.cwd()).resolve()
    directory = Path(directory)
    manifest = _read_sealed(directory / "manifest.json")
    if (manifest.get("schema_version") != "f4s1-manifest-v1" or manifest.get("selection_id") != selection["selection_id"]
            or manifest.get("selection_content_hash") != selection["content_hash"] or manifest.get("revision_id") != directory.name
            or manifest.get("purpose") != selection.get("purpose", "production") or manifest.get("production_eligible") is not False):
        raise ValueError("f4s1_manifest_scope_mismatch")
    required = {"checkpoint.json", "screening_inputs.json", "strategy_config.json", "technical_evaluation.json", "technical_evaluation.csv",
        "strategy_input_readiness.json", "strategy_input_readiness.csv", "eligibility_evidence.json", "eligibility_evidence.csv",
        "candidate_evidence/materials.json", "technical_eligibility_report.json", "technical_eligibility_report.md"}
    if set(manifest.get("files", {})) != required:
        raise ValueError("f4s1_manifest_files_incomplete")
    for name, checksum in manifest["files"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != checksum:
            raise ValueError("f4s1_revision_file_hash_mismatch")
    checkpoint = _read_sealed(directory / "checkpoint.json")
    inputs = _json(directory / "screening_inputs.json")
    from .sector_screening import screening_input_hash
    if (checkpoint["selection_id"] != selection["selection_id"] or checkpoint["selection_content_hash"] != selection["content_hash"]
            or checkpoint["revision_id"] != directory.name or checkpoint["input_hash"] != inputs.get("input_hash")
            or inputs.get("input_hash") != screening_input_hash(inputs) or inputs.get("selection_content_hash") != selection["content_hash"]
            or inputs.get("selection_id") != selection["selection_id"] or inputs.get("mode") != selection["mode"]
            or inputs.get("purpose") != manifest["purpose"] or inputs.get("target_date") != selection["target_date"]
            or checkpoint["strategy_hash"] != digest(_json(directory / "strategy_config.json"))):
        raise ValueError("f4s1_checkpoint_binding_mismatch")
    refs = checkpoint["source_refs"] + inputs.get("file_refs", [])
    for ref in checkpoint["source_refs"]:
        path = _ref(root, ref["path"])
        if path["sha256"] != ref["sha256"]:
            raise ValueError("f4s1_source_changed")
        refs += _json(path["path"]).get("file_refs", [])
    for ref in {ref["path"]: ref for ref in refs}.values():
        if _ref(root, ref["path"])["sha256"] != ref["sha256"]:
            raise ValueError("f4s1_source_changed")
    report = _read_sealed(directory / "technical_eligibility_report.json")
    if (report["selection_id"] != selection["selection_id"] or report["revision_id"] != directory.name
            or report["purpose"] != manifest["purpose"] or report["production_eligible"] is not False
            or report["mode"] != selection["mode"] or report["target_date"] != selection["target_date"]
            or report["selection_content_hash"] != selection["content_hash"]):
        raise ValueError("f4s1_report_scope_mismatch")
    technical = _json(directory / "technical_evaluation.json")
    eligibility = _read_sealed(directory / "eligibility_evidence.json")
    materials = _json(directory / "candidate_evidence/materials.json")
    for package in materials.get("packages", []):
        for document in package.get("documents", []):
            for name in ("raw_source", "collection", "registry_snapshot"):
                ref = document.get("provenance", {}).get(name)
                if not ref or _ref(root, ref["path"])["sha256"] != ref["sha256"]:
                    raise ValueError("f4s1_company_body_source_changed")
    if (technical.get("result_hash") != digest({key: value for key, value in technical.items() if key != "result_hash"})
            or technical.get("input_hash") != inputs["input_hash"] or report["technical_result_hash"] != technical["result_hash"]
            or report["eligibility_content_hash"] != eligibility["content_hash"]
            or report["company_materials_hash"] != digest(materials)
            or {row["security_id"] for row in report["readiness"]} != {row["security_id"] for row in selection["members"]}
            or len(report["readiness"]) != len(selection["members"])):
        raise ValueError("f4s1_report_inputs_mismatch")
    return report


def _summary(report, directory, reused=False):
    return {"status": report["status"], "selection_id": report["selection_id"], "revision_id": directory.name,
        "purpose": report["purpose"], "production_eligible": False, "counts": report["counts"],
        "reused_revision": reused, "output_directory": str(directory),
        "report_path": str(directory / "technical_eligibility_report.md"), "network_requests": 0,
        "database_writes": 0, "model_calls": 0, "model_tokens": 0, "f4s2_ready": False}


def readiness_rows(technical, eligibility, gaps):
    """Explained cache absences never erase missing strategy input dates."""
    eligibility_by_id = {row["security_id"]: row for row in eligibility["evaluations"]}
    explained = gaps.get("definitive_non_trading_dates_by_security", {})
    rows = []
    for row in technical["evaluations"]:
        state = row.get("cache_status", {})
        missing = set(state.get("missing_raw_dates", [])) | set(state.get("missing_adjusted_dates", []))
        known = set(explained.get(row["security_id"], []))
        hard_issues = set(state.get("cache_issues", [])) - {"history_calendar_dates_missing", "complete_adjustment_window_missing"}
        qualification = eligibility_by_id[row["security_id"]]
        rows.append({"security_id": row["security_id"], "symbol": row["symbol"], "name": row.get("name"),
            "cache_target_complete": row.get("cache_target_complete", False),
            "history_window_accounted_for": bool(row.get("cache_target_complete")) or bool(missing and not (missing - known) and not hard_issues),
            "historical_non_trading_dates": sorted(missing & known), "unexplained_dates": sorted(missing - known),
            "cache_status": state, "strategy_inputs_ready": row.get("strategy_inputs_ready", False),
            "rule_input_readiness": row.get("rule_input_readiness", {}),
            "valid_history_count": row["metrics"]["valid_history_count"],
            "adjustment_ready": row.get("adjustment_ready", False),
            "strategy_adjustment_ready": row.get("strategy_adjustment_ready", False),
            "technical_status": row["technical_status"], "eligibility_status": qualification["eligibility_status"],
            "eligibility_gaps": qualification["gaps"], "exclusion_reasons": qualification["exclusion_reasons"],
            "material_queue_status": qualification["material_queue_status"], "data_issues": row.get("data_issues", []),
            "purpose": technical["purpose"], "production_eligible": False})
    return rows


def _markdown(report, technical, eligibility, materials):
    lines = ["# F4-S1 工程技术与资格报告", "", "沪深 A 股·板块精选研究，暂不含北交所。工程材料不代表自然自动入选或已核验投资机会。", "",
        f"冻结编号：{report['selection_id']}；修订：{report['revision_id']}",
        f"目标交易日：{report['target_date']}；本次实际补证截点：{report['cutoff_at']}",
        "当日观察、次日补证及本次历史状态重建分别留存；后取得信息未倒填到原截点。", "",
        "| 证券 | 名称 | 缓存目标 | 日期已解释 | 策略输入 | 技术 | 资格 | 材料队列 |", "|---|---|---|---|---|---|---|---|"]
    for row in report["readiness"]:
        vals = [row["symbol"], row["name"], "完整" if row["cache_target_complete"] else "部分",
            "是" if row["history_window_accounted_for"] else "否", "就绪" if row["strategy_inputs_ready"] else "有缺口",
            row["technical_status"], row["eligibility_status"], row["material_queue_status"]]
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in vals) + " |")
    if not report["readiness"]:
        lines += ["", "生产 S=0：历史、资格及公司材料未触发/不适用，比例 N/A；未借用工程集合。"]
    lines += ["", "320 日仍为缓存目标；策略固定使用原 120 个交易日窗口，不跳过停牌、不填价。MA20/60、20 日均成交额5000万元、相对基准21个对齐点均未改变。",
        "停牌证据只解释缺日，没有新增 K 线；全部缓存完整性与策略输入就绪分开统计。", ""]
    qualifiers = {row["security_id"]: row for row in eligibility["evaluations"]}
    for row in technical["evaluations"]:
        lines += ["## " + row["symbol"], "", "| 指标 | 值 |", "|---|---:|"]
        for name, value in row["metrics"].items():
            lines.append(f"| {name} | {'无法计算' if value is None else value} |")
        lines += ["", "| 条件 | 状态 | 原因 |", "|---|---|---|"]
        for condition in row["technical_conditions"] + qualifiers[row["security_id"]]["conditions"]:
            lines.append("| " + " | ".join(str(condition[key]).replace("|", "\\|") for key in ("label", "status", "reason")) + " |")
        lines += ["", "数据来源、复权锚点、窗口版本及所有字段证据见同目录 JSON/CSV。", ""]
    lines += ["## 公司材料与剩余条件", "", "仅复用许可范围内的本地正文索引；未查到不等于没有风险。具体证据、定位和权限缺口见 candidate_evidence/materials.json。",
        "公司正文不足时不得进入模型解释。行业标签不作为主营业务证据。", "",
        "全部工程对象的生产许可、正式候选数、模型队列、模型调用和 Token 均为 0；未启用通知或定时任务。",
        "原 2026-09-11 生产预关注=0、关注=0、S=0 保持，生产自动非空链路没有自然发生。", ""]
    return "\n".join(lines)


def _execute(root, selection, config, directory, *, interrupt_after=None):
    from .sector_screening import evaluate_selection, screening_input_hash
    from .sector_eligibility import read_field_facts, evaluate_sector_eligibility
    from .sector_gap_diagnosis import read_gap_diagnosis
    from .sector_company_evidence import prepare_company_materials
    checkpoint = _read_sealed(directory / "checkpoint.json")
    inputs, strategy = _json(directory / "screening_inputs.json"), _json(directory / "strategy_config.json")
    if (checkpoint["selection_content_hash"] != selection["content_hash"] or checkpoint["selection_id"] != selection["selection_id"]
            or checkpoint["revision_id"] != directory.name or inputs.get("input_hash") != screening_input_hash(inputs)
            or checkpoint["input_hash"] != inputs["input_hash"] or checkpoint["strategy_hash"] != digest(strategy)):
        raise ValueError("f4s1_checkpoint_binding_mismatch")
    for ref in checkpoint["source_refs"] + inputs.get("file_refs", []):
        if _ref(root, ref["path"])["sha256"] != ref["sha256"]:
            raise ValueError("f4s1_source_changed")
    if _company_basis(root, selection) != checkpoint["company_basis"]:
        raise ValueError("f4s1_company_sources_changed_create_new_revision")
    for name, expected in inputs.get("implementation_hashes", {}).items():
        if hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() != expected:
            raise ValueError("f4s1_implementation_changed_create_new_revision")
    # Resume from frozen files, but still authenticate all external proof bodies.
    if selection["members"]:
        gaps = read_gap_diagnosis(root, checkpoint["gap_path"], selection, config)
        facts = read_field_facts(root, checkpoint["facts_path"], selection)
    else:
        gaps, facts = {}, None
    technical = evaluate_selection(selection, inputs, strategy)
    technical.update(production_eligible=False, formal_verified_opportunity_count=0)
    technical["result_hash"] = digest({key: value for key, value in technical.items() if key != "result_hash"})
    write_new(directory / "technical_evaluation.json", technical)
    if interrupt_after == "technical":
        raise InterruptedError("f4s1_checkpoint_saved_after_technical")
    cutoff = inputs["cutoff_at"]
    eligibility = evaluate_sector_eligibility(selection, technical, facts, cutoff_at=cutoff) if facts is not None else _seal({
        "schema_version": "f4s1-eligibility-result-v1", "selection_id": selection["selection_id"], "evaluations": [],
        "purpose": selection.get("purpose", "production"), "production_eligible": False, "counts": {"stock_count": 0,
        "pass_count": 0, "fail_count": 0, "pending_count": 0}, "status": "not_applicable", "model_calls": 0})
    materials = prepare_company_materials(root, selection, technical, eligibility, cutoff_at=cutoff) if selection["members"] else _seal({
        "schema_version": "f4s1-company-materials-v1", "selection_id": selection["selection_id"], "purpose": "production", "production_eligible": False,
        "status": "not_applicable", "packages": [], "model_calls": 0, "model_tokens": 0})
    rows = readiness_rows(technical, eligibility, gaps)
    counts = {"stock_count": len(rows), "cache_complete": sum(row["cache_target_complete"] for row in rows),
        "history_window_accounted_for": sum(row["history_window_accounted_for"] for row in rows),
        "strategy_inputs_ready": sum(row["strategy_inputs_ready"] for row in rows),
        "adjustment_ready": sum(row["adjustment_ready"] for row in rows),
        "strategy_adjustment_ready": sum(row["strategy_adjustment_ready"] for row in rows),
        **{"technical_" + state: sum(row["technical_status"] == state for row in rows) for state in ("pass", "fail", "unknown", "not_applicable")},
        **{"eligibility_" + state: sum(row["eligibility_status"] == state for row in rows) for state in ("pass", "fail", "pending")},
        "local_material_queue": sum(row["material_queue_status"] in {"local_evidence_preparation", "diagnostic_pending"} for row in rows),
        "formal_candidates": 0, "model_queue": 0}
    report = _seal({"schema_version": "f4s1-report-v1", "status": "engineering_partial" if rows else "not_applicable",
        "selection_id": selection["selection_id"], "selection_content_hash": selection["content_hash"], "revision_id": directory.name,
        "purpose": selection.get("purpose", "production"), "production_eligible": False, "mode": selection["mode"],
        "market_scope": "sse_szse_a", "research_mode": "sector_first", "target_date": selection["target_date"],
        "cutoff_at": cutoff, "historical_reconstruction": True, "readiness": rows, "counts": counts,
        "technical_result_hash": technical["result_hash"], "eligibility_content_hash": eligibility["content_hash"],
        "company_materials_hash": digest(materials), "network_requests": 0, "database_writes": 0,
        "new_price_rows": 0, "model_calls": 0, "model_tokens": 0, "f4s2_ready": False})
    values = {"checkpoint.json": checkpoint, "screening_inputs.json": inputs, "strategy_config.json": strategy,
        "technical_evaluation.json": technical, "strategy_input_readiness.json": _seal({"selection_id": selection["selection_id"],
        "purpose": report["purpose"], "production_eligible": False, "rows": rows}), "eligibility_evidence.json": eligibility,
        "candidate_evidence/materials.json": materials, "technical_eligibility_report.json": report}
    hashes = {name: write_new(directory / name, value) for name, value in values.items()}
    hashes["technical_evaluation.csv"] = _csv(directory / "technical_evaluation.csv", technical["evaluations"])
    hashes["strategy_input_readiness.csv"] = _csv(directory / "strategy_input_readiness.csv", rows)
    hashes["eligibility_evidence.csv"] = _csv(directory / "eligibility_evidence.csv", [dict(fact, eligibility_status=row["eligibility_status"])
        for row in eligibility["evaluations"] for fact in row["facts"]])
    hashes["technical_eligibility_report.md"] = _bytes(directory / "technical_eligibility_report.md", _markdown(report, technical, eligibility, materials).encode())
    write_new(directory / "manifest.json", _seal({"schema_version": "f4s1-manifest-v1", "selection_id": selection["selection_id"],
        "selection_content_hash": selection["content_hash"], "revision_id": directory.name,
        "purpose": report["purpose"], "production_eligible": False, "files": hashes}))
    return _summary(read_revision(directory, selection, root=root), directory)


def prepare_revision(root, selection_id, *, gap_path=None, facts_path=None, revision=None,
                     validation_config_path="config/sector_validation.json", strategy_config_path="config/sector_screening_f4s1.json",
                     report_only=False, dry_run=False, interrupt_after=None):
    from .sector_screening import validate_screening_config, screening_input_hash
    root = Path(root).resolve()
    selection, config = _selection(root, selection_id, validation_config_path)
    if selection.get("purpose") != "engineering_validation" and selection["members"]:
        raise ValueError("f4s1_nonempty_production_not_enabled")
    base = _base(root, selection, config)
    if dry_run:
        return {"status": "dry_run", "selection_id": selection_id, "denominator": len(selection["members"]), "production_eligible": False,
            "network_requests": 0, "database_writes": 0, "model_calls": 0, "model_tokens": 0}
    if revision:
        if not re.fullmatch(r"f4s1-[0-9a-f]{24}", revision):
            raise ValueError("f4s1_invalid_revision")
        directory = base / revision
        if report_only or (directory / "manifest.json").is_file():
            return _summary(read_revision(directory, selection, root=root), directory, reused=True)
        return _execute(root, selection, config, directory, interrupt_after=interrupt_after)
    strategy = validate_screening_config(_json(local_path(root, strategy_config_path)))
    if strategy["data_semantics_version"] != "f4s1-cache-strategy-input-split-v1":
        raise ValueError("f4s1_requires_explicit_strategy_semantics")
    if selection["members"]:
        from .sector_gap_diagnosis import read_gap_diagnosis
        from .sector_eligibility import read_field_facts
        from .sector_history import screening_history_inputs
        from .sector_benchmark import read_benchmark
        gaps = read_gap_diagnosis(root, gap_path, selection, config)
        facts = read_field_facts(root, facts_path, selection)
        history = screening_history_inputs(root, selection, config)
        benchmark = read_benchmark(root, selection, config)
        source_refs = [_ref(root, gap_path), _ref(root, facts_path)]
        proof_hashes = [gaps["content_hash"], facts["content_hash"]]
    else:
        history = {"calendar": {"verified": True, "trading_dates": [], "issues": []}, "securities": {}, "file_refs": [], "issues": []}
        benchmark = {"status": "not_applicable", "records": [], "issues": [], "file_refs": []}
        source_refs, proof_hashes = [], []
    implementation = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() for name in (
        "sector_f4s1.py", "sector_screening.py", "sector_eligibility.py", "sector_gap_diagnosis.py", "sector_company_evidence.py", "factors/trend.py")}
    company_basis = _company_basis(root, selection)
    basis = {"selection_hash": selection["content_hash"], "history": history, "benchmark": benchmark,
        "strategy": strategy, "implementation": implementation, "proof_hashes": proof_hashes, "company_basis": company_basis}
    revision = "f4s1-" + digest(basis)[:24]
    directory = base / revision
    if (directory / "manifest.json").is_file():
        return _summary(read_revision(directory, selection, root=root), directory, reused=True)
    if (directory / "checkpoint.json").is_file():
        return _execute(root, selection, config, directory, interrupt_after=interrupt_after)
    inputs = {"schema_version": "f3s-screening-input-v1", "mode": selection["mode"], "purpose": selection.get("purpose", "production"),
        "selection_id": selection_id, "selection_content_hash": selection["content_hash"], "target_date": selection["target_date"],
        "cutoff_at": datetime.now(SHANGHAI).isoformat(), "calendar": history["calendar"], "securities": history["securities"],
        "benchmark": benchmark, "file_refs": history.get("file_refs", []) + benchmark.get("file_refs", []),
        "historical_reconstruction": True, "source_cutoff_at": selection.get("source_cutoff_at", selection["cutoff_at"]),
        "implementation_hashes": implementation, "history_issues": history.get("issues", [])}
    inputs["input_hash"] = screening_input_hash(inputs)
    checkpoint = _seal({"schema_version": "f4s1-checkpoint-v1", "selection_id": selection_id, "selection_content_hash": selection["content_hash"],
        "revision_id": revision, "input_hash": inputs["input_hash"], "strategy_hash": digest(strategy), "source_refs": source_refs,
        "company_basis": company_basis,
        "gap_path": str(source_refs[0]["path"]) if source_refs else None, "facts_path": str(source_refs[1]["path"]) if source_refs else None,
        "purpose": inputs["purpose"], "production_eligible": False, "created_at": inputs["cutoff_at"], "state": "inputs_frozen"})
    write_new(directory / "screening_inputs.json", inputs)
    write_new(directory / "strategy_config.json", strategy)
    write_new(directory / "checkpoint.json", checkpoint)
    return _execute(root, selection, config, directory, interrupt_after=interrupt_after)


def run_f4s1(root, args):
    root = Path(root).resolve()
    operation = args.sector_command
    if operation in {"gaps", "qualify"}:
        selection, config = _selection(root, args.selection, args.validation_config)
        if selection.get("purpose") != "engineering_validation":
            raise ValueError("f4s1_source_diagnostics_require_engineering_selection")
        if args.dry_run:
            return {"status": "dry_run", "selection_id": args.selection, "denominator": len(selection["members"]), "network_requests": 0, "model_calls": 0}, 0
        if operation == "gaps":
            from .sector_gap_diagnosis import diagnose_history_gaps
            result = diagnose_history_gaps(root, selection, config, online=args.online, source_revision=args.source_revision, max_seconds=args.max_seconds)
            return {key: result.get(key) for key in ("revision_run_id", "json_path", "csv_path", "metrics", "model_calls")}, 0
        from .sector_eligibility import read_field_facts, collect_sector_eligibility
        if args.eligibility_evidence:
            result = read_field_facts(root, args.eligibility_evidence, selection)
        else:
            result = collect_sector_eligibility(root, selection, online=args.online, max_seconds=args.max_seconds,
                output_directory=_base(root, selection, config).parent / "qualifications" / datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f"))
        return {"status": "qualification_facts_archived", "selection_id": args.selection, "path": result.file_ref["path"],
            "source_health": result["source_health"], "model_calls": 0, "model_tokens": 0}, 0
    summary = prepare_revision(root, args.selection, gap_path=getattr(args, "gap_diagnosis", None),
        facts_path=getattr(args, "eligibility_evidence", None), revision=getattr(args, "revision", None),
        validation_config_path=args.validation_config, strategy_config_path=args.strategy_config,
        dry_run=args.dry_run, report_only=operation == "f4s1-report")
    return summary, 0
