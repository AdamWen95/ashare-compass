"""M3 orchestration: deterministic collection/input, one model, guarded publication."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .contracts import DailyResearchOutput, Evidence, ResearchOutput, SHANGHAI, SourceRegistration, aware_time
from .evidence import EvidenceStore, digest, load_local_materials, select_evidence, validate_claims
from .model import ChatCompletionsModel, choose_response_mode, complete_validated
from .model_settings import ModelSettings, load_model_settings
from .preparation import fit_prompt_budget, load_research_config, market_baseline, metric_registry, prepare_model_input
from ashare_daily.reports.m21_render import write_m21_audit_csv
from ashare_daily.reports.m3_render import render_m3_html, render_m3_markdown, write_claim_audit


def _write(path: Path, value, *, exclusive: bool = True):
    with path.open("x" if exclusive else "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def _root(output_dir: Path, offline: bool) -> Path:
    root = Path(output_dir).resolve()
    if {"demo", "fixtures"}.intersection(part.lower() for part in root.parts):
        raise ValueError("M3 研究输出不能混入 DEMO/fixtures")
    return root / ("offline_test" if offline else "research") / "m3"


def _interval(start: str, cutoff: str) -> tuple[str, str]:
    first, last = aware_time(start), aware_time(cutoff)
    if first >= last or last > datetime.now(SHANGHAI):
        raise ValueError("资讯区间须起点早于截点，截点不得晚于当前北京时间")
    return first.isoformat(), last.isoformat()


def archive_materials(*, collection: dict | None = None, import_file: Path | None = None,
                      database: Path = Path("data/research/market.sqlite3"), output_dir: Path = Path("outputs"),
                      start: str, cutoff: str, offline: bool = False) -> dict:
    """Store real and manual acquisitions separately; preserve first observations."""
    start, cutoff = _interval(start, cutoff)
    sources, evidence, health = [], [], []
    collection = deepcopy(collection or {})
    if collection:
        sources.extend(collection["sources"])
        evidence.extend(collection.get("all_evidence", collection["evidence"]))
        health.extend(collection["source_health"])
    imported = None
    if import_file:
        imported = load_local_materials(import_file)
        sources.extend(imported["sources"])
        evidence.extend(imported["evidence"])
        health.extend({"source_id": source["source_id"], "status": "manual_import", "category": source["category"],
                       "coverage": "人工提供资料，不代表自动采集已验证"} for source in imported["sources"])
    if not sources:
        raise ValueError("缺少已登记来源；先 collect-materials 或导入真实出处资料")
    store = EvidenceStore(database, verification_kind="offline_test" if offline else "real")
    source_map = {}
    for source in sources:
        source = SourceRegistration.model_validate(source).model_dump(mode="json")
        if source["source_id"] in source_map and source_map[source["source_id"]] != source:
            raise ValueError("同次运行来源登记冲突，不能覆盖自动来源许可")
        source_map[source["source_id"]] = source
    source_list = list(source_map.values())
    store.register_sources(source_list)
    stored = store.ingest(evidence)
    # Query only the active registered scope, including earlier cached documents
    # for explicitly labeled background. No market rows are changed.
    catalog = [e for e in store.list_evidence() if e["source_id"] in source_map]
    # A corrected parser can change metadata precision for identical content.
    # Prefer this acquisition's version without deleting any original evidence.
    current_versions = {(e["source_id"], e["original_url"], e["content_hash"]): e["evidence_id"] for e in evidence}
    superseded = [e["evidence_id"] for e in catalog if current_versions.get(
        (e["source_id"], e["original_url"], e["content_hash"]), e["evidence_id"]) != e["evidence_id"]]
    catalog = [e for e in catalog if e["evidence_id"] not in superseded]
    selection = select_evidence(catalog, start, cutoff, allow_historical_reconstruction=True)
    root = _root(output_dir, offline) / "materials"
    root.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "m3-material-bundle-v1", "verification_kind": "offline_test" if offline else "real_materials",
               "created_at": datetime.now(SHANGHAI).isoformat(), "query_start": start, "cutoff": cutoff,
               "sources": source_list, "evidence": catalog, "source_health": health,
               "collection": collection, "manual_import": imported, "storage": stored,
               "selection_summary": {k: v for k, v in selection.items() if k not in {"eligible"}},
               "superseded_metadata_versions": superseded,
               "database_path": str(Path(database).resolve())}
    payload["bundle_hash"] = digest(payload)
    path = root / ("materials-" + payload["bundle_hash"] + ".json")
    _write(path, payload)
    return {"status": "partial" if selection["excluded"] or any(h.get("status") != "ok" for h in health) else "ok",
            "bundle_file": str(path), "bundle_hash": payload["bundle_hash"], "stored": stored,
            "evidence_count": len(catalog), "eligible_count": selection["eligible_count"],
            "source_health": health, "historical_reconstruction": selection["historical_reconstruction"]}


def read_material_bundle(path: Path, *, offline: bool = False) -> dict:
    from ashare_daily.operations.paths import resolve_input_path
    path = resolve_input_path(path)
    if Path(path).stat().st_size > 30_000_000:
        raise ValueError("资料包超出本轮小批量限制")
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if payload.get("schema_version") != "m3-material-bundle-v1":
        raise ValueError("不是已归档 M3 资料包；人工资料须先导入")
    if payload.get("bundle_hash") != digest({k: v for k, v in payload.items() if k != "bundle_hash"}):
        raise ValueError("资料包哈希不匹配，拒绝已修改证据")
    if payload.get("verification_kind") != ("offline_test" if offline else "real_materials"):
        raise ValueError("真实行情与离线模拟资料不能混用")
    for item in payload["evidence"]:
        Evidence.model_validate(item)
        if item["content_hash"] != digest(item["content"]):
            raise ValueError("资料正文哈希不匹配")
        if (item["acquisition_mode"] == "offline_test") != offline:
            raise ValueError("资料包包含不同验证类型")
    return payload


def _empty_analysis(reason: str) -> dict:
    return {"status": "not_run", "accepted_claims": [], "rejected_claims": [], "claim_evidence_rows": [],
            "accepted_count": 0, "rejected_count": 0, "semantic_review_required": True, "reason": reason}


def _publish(report: dict, root: Path, snapshot: dict, model_run: dict) -> dict:
    now = datetime.now(SHANGHAI)
    directory = root / report["trade_date"] / (now.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    _write(directory / "input_snapshot.json", snapshot)
    _write(directory / "model_responses.json", model_run)
    _write(directory / "daily_brief.json", report)
    for name, content in (("daily_brief.md", render_m3_markdown(report)), ("daily_brief.html", render_m3_html(report))):
        with (directory / name).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    write_m21_audit_csv(report["market"], directory / "screening_audit.csv")
    write_claim_audit(report, directory / "claim_evidence_audit.csv")
    _write(directory / "evidence_catalog.json", {"evidence": report["evidence_catalog"], "sources": report["source_registry"], "coverage": report["coverage"]})
    names = ("daily_brief.json", "daily_brief.md", "daily_brief.html", "screening_audit.csv", "claim_evidence_audit.csv", "evidence_catalog.json", "input_snapshot.json", "model_responses.json")
    _write(directory / "manifest.json", {"schema_version": "m3-report-manifest-v1", "input_snapshot_id": report["input_snapshot_id"],
        "result_hash": report["result_hash"], "files": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names}})
    result = {"workflow_version": "m3", "generation_status": "ok", "status": report["status"], "statuses": report["statuses"],
              "trade_date": report["trade_date"], "actual_market_date": report["actual_market_date"], "cutoff_at": report["cutoff_at"],
              "historical_reconstruction": report["historical_reconstruction"], "verification_kind": report["verification_kind"],
              "research_object_count": len(report["research_objects"]), "accepted_claim_count": len(report["analysis"]["accepted_claims"]),
              "evidence_count": len(report["evidence_catalog"]), "model_input_evidence_count": report["coverage"]["model_input_evidence_count"],
              "model_call_count": model_run.get("call_count", 0), "input_snapshot_id": report["input_snapshot_id"],
              "result_hash": report["result_hash"], "run_directory": str(directory),
              **{key: str(directory / name) for key, name in {"html": "daily_brief.html", "markdown": "daily_brief.md", "json": "daily_brief.json",
                  "csv": "screening_audit.csv", "claim_audit_csv": "claim_evidence_audit.csv", "evidence_catalog": "evidence_catalog.json", "snapshot_path": "input_snapshot.json"}.items()}}
    _write(directory / "result.json", result)
    _write(root / "latest.json", result, exclusive=False)
    return result


def replay_research(path: Path, output_dir: Path = Path("outputs")) -> dict:
    directory = Path(path).resolve()
    if directory.is_file():
        directory = directory.parent
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    expected_names = {"daily_brief.json", "daily_brief.md", "daily_brief.html", "screening_audit.csv", "claim_evidence_audit.csv", "evidence_catalog.json", "input_snapshot.json", "model_responses.json"}
    if manifest.get("schema_version") != "m3-report-manifest-v1" or set(manifest.get("files", {})) != expected_names:
        raise ValueError("缺少可核验 M3 存档清单")
    for name, value in manifest["files"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != value:
            raise ValueError("存档文件哈希不匹配，拒绝改写后的重放")
    report = json.loads((directory / "daily_brief.json").read_text(encoding="utf-8"))
    snapshot = json.loads((directory / "input_snapshot.json").read_text(encoding="utf-8"))
    model_run = json.loads((directory / "model_responses.json").read_text(encoding="utf-8"))
    if snapshot["snapshot_id"] != "m3-" + digest({k: v for k, v in snapshot.items() if k != "snapshot_id"}):
        raise ValueError("研究输入快照哈希不匹配")
    if report["result_hash"] != manifest["result_hash"]:
        raise ValueError("报告结果标识与清单不符")
    if report["result_hash"] != digest({k: v for k, v in report.items() if k not in {"result_hash", "replayed_at"}}):
        raise ValueError("报告内容哈希不匹配")
    if not report["input_snapshot_id"] == snapshot["snapshot_id"] == manifest["input_snapshot_id"]:
        raise ValueError("报告、清单与输入快照标识不一致")
    report["replayed_at"] = datetime.now(SHANGHAI).isoformat()
    return _publish(report, _root(output_dir, report["verification_kind"] == "offline_test"), snapshot, model_run)


def run_research(*, market_snapshot: Path | str | None = None, evidence_bundle: Path, start: str, cutoff: str,
                 config_path: Path = Path("config/m3.json"), env_file: Path = Path(".env"),
                 output_dir: Path = Path("outputs"), model_capabilities: Path | None = None,
                 skip_model: bool = False, client: ChatCompletionsModel | None = None) -> dict:
    start, cutoff = _interval(start, cutoff)
    config = load_research_config(config_path)
    frozen, market, market_path = market_baseline(market_snapshot)
    offline = frozen.get("verification_kind") == "offline_test"
    if client is not None and client.verification_kind == "offline_test" and not offline:
        raise ValueError("真实行情不能使用模拟模型冒充研究")
    if offline and not skip_model and (client is None or client.verification_kind != "offline_test"):
        raise ValueError("离线资料只能用显式离线模型客户端测试，不能发起真实模型请求")
    bundle = read_material_bundle(evidence_bundle, offline=offline)
    selection = select_evidence(bundle["evidence"], start, cutoff, allow_historical_reconstruction=config.allow_historical_reconstruction)
    prepared = prepare_model_input(selection["eligible"], bundle["sources"], market, config)
    metrics = metric_registry(market)
    configuration_error = None
    try:
        settings = client.settings if client else load_model_settings(env_file)
    except ValueError:
        settings = ModelSettings()
        configuration_error = "invalid_model_configuration"
    client = client or ChatCompletionsModel(settings)
    try:
        mode = choose_response_mode(settings, model_capabilities)
    except (ValueError, OSError):
        mode = "text"
        configuration_error = "invalid_model_capabilities"
    output_model = DailyResearchOutput if config.prompt_version == 'evidence-research-v1.1.1' else ResearchOutput
    prepared, messages = fit_prompt_budget(prepared, market, config, output_model.model_json_schema(), settings.max_input_chars)
    allowed_symbols = {obj["symbol"]: obj["name"] for obj in prepared["research_objects"]}
    model_run = {"status": configuration_error or ("skipped" if skip_model else "no_eligible_evidence"), "configuration": settings.public_dict(),
                 "response_mode": mode, "responses": [], "format_repairs": 0}
    analysis = _empty_analysis(model_run["status"])
    if not skip_model and not configuration_error and prepared["evidence"]:
        result = complete_validated(client, messages, output_model, response_mode=mode)
        model_run.update(result)
        parsed = model_run.pop("parsed", None)
        if result["status"] == "ok":
            if len(parsed["claims"]) > config.max_claims:
                model_run["status"] = "claim_budget_exceeded"
            else:
                # Only excerpts actually admitted to the prompt can support claims.
                analysis = validate_claims(parsed, prepared["evidence"], allowed_symbols, metrics, cutoff,
                                          strict_counterevidence=config.prompt_version == 'evidence-research-v1.1.1')
                if analysis["status"] == "no_valid_claims":
                    model_run["status"] = "evidence_validation_failed"
                elif analysis["status"] == "partial":
                    model_run["status"] = "partial_validated"
    model_run.update(client.summary())
    model_run["calls"] = client.records
    # Unvalidated output belongs to the forensic archive, never readable research.
    model_run["validation_audit"] = deepcopy(analysis)
    analysis["rejected_claims"] = [{"claim_id": row.get("claim_id"), "validation_reasons": row.get("validation_reasons", [])}
                                   for row in analysis["rejected_claims"]]
    analysis["claim_evidence_rows"] = [row if row.get("validation_status") == "accepted" else
        {k: v for k, v in row.items() if k in {"claim_id", "evidence_id", "validation_status", "validation_reasons"}}
        for row in analysis["claim_evidence_rows"]]
    if not client.call_count:
        model_run["verification_kind"] = "not_run"
    health = bundle["source_health"]
    message_status = "partial" if selection["excluded"] or any(h.get("status") != "ok" for h in health) else "ok" if prepared["evidence"] else "missing"
    statuses = {"market": market["status"], "messages": message_status, "model": model_run["status"],
                "generation": "ok", "citation_validation": analysis["status"], "semantic_review": "人工语义复核未完成"}
    now = datetime.now(SHANGHAI).isoformat()
    snapshot = {"schema_version": "m3-input-v1", "frozen_at": now, "market_snapshot_path": str(market_path),
                "market_input": frozen, "material_bundle": bundle, "query_start_at": start, "cutoff_at": cutoff,
                "research_config": config.model_dump(mode="json"), "model_configuration": settings.public_dict(),
                "model_response_mode": mode, "prompt_version": config.prompt_version, "actual_model_messages": messages,
                "actual_model_input": prepared}
    snapshot["snapshot_id"] = "m3-" + digest(snapshot)
    gaps = ["原量价/资格基线仍有缺口：" + str(market["counts"]["stocks_with_data_gaps"]) + " 只股票；M3 不改变其资格或量价结论。",
            "来源为有限登记页面或人工资料，不代表完整新闻、上市公司公告和研报覆盖。",
            "语义正确性不能由 JSON、引用存在或摘录匹配保证，研究推论需人工抽查。"]
    if model_run["status"] not in {"ok", "partial_validated"}:
        gaps.append("模型分析失败/跳过：" + model_run["status"] + "；未以模板或模拟回复替代真实分析。")
    gaps.extend(f"{h.get('source_id')}：{h.get('status')}；{h.get('failures', h.get('coverage', ''))}" for h in health if h.get("status") != "ok")
    coverage = {"acquired_catalog_count": len(bundle["evidence"]), "time_eligible_count": selection["eligible_count"],
                "model_input_evidence_count": len(prepared["evidence"]), "model_input_characters": prepared["input_characters"],
                "complete_prompt_characters": prepared["complete_prompt_characters"],
                "independent_event_count": selection["independent_event_count"], "new_event_count": selection["new_event_count"],
                "background_count": selection["background_count"], "time_or_quality_exclusions": selection["excluded"],
                "input_exclusions": prepared["skipped_evidence"], "outside_scope_leads": prepared["outside_scope_leads"],
                "content_type_counts": {kind: sum(e["content_type"] == kind for e in selection["eligible"]) for kind in ("metadata_only", "abstract", "fulltext")}}
    catalog = list(selection["eligible"])
    metadata_ids = {row["evidence_id"] for row in selection["excluded"] if row["reason"] == "metadata_only_not_read_body"}
    catalog.extend({**item, "analysis_use": "仅目录，未送模型"} for item in bundle["evidence"] if item["evidence_id"] in metadata_ids)
    source_map = {source["source_id"]: source for source in bundle["sources"]}
    catalog = deepcopy(catalog)
    for item in catalog:
        if not source_map[item["source_id"]]["publish_excerpt_allowed"]:
            item["content"] = ""
            item["report_content_redacted"] = "报告摘录许可未确认；正文仅保留于允许缓存的内部存档"
    report = {"schema_version": "m3-report-v1", "title": "今日方向简报 · M3 证据研究增强",
              "notice": "部分覆盖研究版；历史资格缺口保留，模块状态分别展示。" + ("模型分析未完成，不能称为已通过真实模型研究验收。" if model_run["status"] not in {"ok", "partial_validated"} else "研究推论须与引用原文共同核查。"),
              "status": "partial", "verification_kind": "offline_test" if offline else "local_real_data",
              "trade_date": market["trade_date"], "actual_market_date": market["actual_market_date"], "query_start_at": start,
              "cutoff_at": cutoff, "actual_generated_at": now, "historical_reconstruction": selection["historical_reconstruction"],
              "statuses": statuses, "market": market, "metric_registry": metrics, "research_config": config.model_dump(mode="json"),
              "research_objects": prepared["research_objects"], "source_registry": bundle["sources"], "source_health": health,
              "evidence_catalog": catalog, "analysis": analysis,
              "model_run": {k: v for k, v in model_run.items() if k not in {"calls", "responses", "validation_audit"}},
              "prompt_version": config.prompt_version, "input_snapshot_id": snapshot["snapshot_id"], "coverage": coverage, "gaps": gaps}
    report["result_hash"] = digest(report)
    return _publish(report, _root(output_dir, offline), snapshot, model_run)
