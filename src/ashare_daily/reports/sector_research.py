"""Manual F4-S1 reports from authenticated frozen inputs; no network/model API."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from ashare_daily.artifact_purpose import is_production_artifact, is_production_path
from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.operations.daily import local_path
from ashare_daily.operations.paths import resolve_archived_path
from ashare_daily.sector_selection import digest, verify_selection
from .m3_render import render_sections_html, render_sections_markdown
from .reader import number

from .sector_contracts import SCHEMA, validate_report, validate_report_inputs, expected_history
MANIFEST_SCHEMA = "f4s1-sector-report-manifest-v1"
FILES = {"sector_research_report.json", "sector_research_report.md", "sector_research_report.html",
         "sector_selection.json", "report_inputs.json", "eligibility_fields.csv", "history_gap_accounting.csv"}
REASONS = {"nonpositive_daily_change_or_amount": "行业均涨跌幅或成交额未大于0，未进入预关注",
    "outside_preselection_rank_limit": "超出预关注排名范围", "median_or_breadth_below_gate": "涨跌幅中位数或上涨家数占比未满足规则",
    "outside_selected_rank_limit": "超出关注排名范围", "selection_inputs_blocked": "来源核验未通过",
    "passed_daily_observation_rule": "通过当日观察规则", "no_in_scope_members": "没有范围内普通A股成分"}
FIELD_LABELS = {"identity": "证券身份与范围", "listed": "目标日上市", "st": "目标日ST/风险警示",
                "suspended": "目标日全天停牌", "delisting_period": "目标日退市整理"}
METRIC_LABELS = {"adjusted_close": "前复权收盘（元）", "display_close": "未复权收盘（元）", "ma20": "MA20（元）", "ma60": "MA60（元）",
    "stock_return_20": "股票20日收益", "benchmark_return_20": "基准20日收益", "relative_return_20": "20日超额收益", "avg_amount_20_cny": "20日均成交额（元）", "valid_history_count": "固定120日窗口内有效行情日"}


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _file_ref(root, locator):
    raw = Path(locator)
    path = local_path(root, resolve_archived_path(raw if raw.is_absolute() else root/raw, anchor=root))
    # The existing complete 5,218-member input is 39 MB. This bound applies to
    # authenticated source archives; public viewer export limits stay unchanged.
    if path.stat().st_size > 64_000_000 or any(part.lower() in {".env", ".git", "secrets"} for part in path.parts):
        raise ValueError("sector_report_source_path_not_allowed")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _refs(root, values):
    """Collect and verify declared local references, including archived HTTP body."""
    found, pending = {}, list(values)
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            reference = None
            if isinstance(value.get("path"), str) and re.fullmatch(r"[a-f0-9]{64}", str(value.get("sha256", ""))):
                reference = {"path": value["path"], "sha256": value["sha256"]}
            elif isinstance(value.get("body_path"), str) and value.get("body_sha256"):
                reference = {"path": value["body_path"], "sha256": value["body_sha256"]}
            if reference:
                actual = _file_ref(root, reference["path"])
                if actual["sha256"] != reference["sha256"]:
                    raise ValueError("sector_report_source_hash_mismatch")
                if actual["path"] not in found:
                    found[actual["path"]] = actual
                    if Path(actual["path"]).suffix == ".json":
                        pending.append(_json(actual["path"]))
            else:
                pending.extend(value.values())
    return list(sorted(found.values(), key=lambda row: row["path"]))


def _base(root, selection, config):
    if selection.get("purpose") == "engineering_validation":
        return local_path(root, config["output_directory"])/selection["selection_id"]/"f4s1/reports"
    return root/"outputs/research/sse_szse_a/sector_reports"/selection["selection_id"]



def _sections(report):
    counts = report["counts"]
    sections = [["研究范围与时间", [["paragraph", report["conclusion"]], ["table", [["项目", "本次记录"], [
        ["目标交易日", report["trade_date"]], ["原冻结/补证截点", report["source_cutoff_at"]],
        ["本报告实际生成", report["actual_generated_at"]], ["证券发现范围", "沪深四板块普通A股，暂不含北交所"],
        ["分类体系", report["taxonomy"]], ["行业体系口径", "交易所网站行业门类，独立版本映射；不是上市板块，也不是主题预测"],
        ["用途", "生产零关注日报" if report["purpose"] == "production" else "独立工程诊断，禁止进入生产候选"],
        ["名单发现数（本次历史观察）", report["universe_count"]],
        ["行业目录/预关注/关注/S", f"{counts['catalog_count']} / {counts['preselected_count']} / {counts['selected_count']} / {counts['stock_count']}"],
        ["正式候选/模型队列/调用/Token", "0 / 0 / 0 / 0"]]]],
        ["paragraph", "源业务日期为null时保留null；实际观察、后来取得的状态和本次生成时间分别留存。历史重建不冒充9月11日实时可知。"]]]]
    if report["purpose"] == "production":
        params = report["parameters"]
        sections.append(["行业比较与原观察规则", [
            ["paragraph", f"初排最多{params['preselect_limit']}个行业，来源均涨跌幅与成交额须严格大于0；预关注成分中位数严格大于0，上涨家数占比至少{params['advancing_fraction_min']}，关注最多{params['selected_limit']}个。门槛保持不变。"],
            ["paragraph", "均涨跌幅为完整、有日期的范围内成分算术均值；成交额为人民币元，不能解释成资金净流入。没有补造5/20日行业趋势。"],
            ["table", [["行业", "范围成分", "有效报价/应有", "有据停牌", "均涨跌幅(%)", "中位数(%)", "上涨比例", "总成交额(元)", "未入选原因"],
                [[row["name"], row["scope_member_count"], f"{row['valid_quote_count']}/{row['expected_quote_count']}",
                  len(row["full_day_halted"]), row["ranking_change_pct"], row["median_change_pct"], row["advancing_fraction"],
                  row["ranking_amount_cny"], "；".join(row["reason_texts"])] for row in report["sector_comparison"]]]]]])
        sections.append(["本期研究队列", [["paragraph", "预关注=0、关注=0、S=0。个股历史、技术筛选、资格和公司材料未触发/不适用；比例N/A。"],
            ["paragraph", "本分类体系下无行业满足当前观察规则。不覆盖未关注板块以外的个股机会，不能据此推断整个市场没有机会。"],
            ["paragraph", "主题通道未启用；没有以工程行业或旧固定样本补足生产队列。"]]])
    else:
        sections.append([f"{len(report['evaluations'])}只冻结成员的独立状态", [["table", [["证券", "名称", "缓存", "按有据状态应有/已有", "策略输入", "技术", "资格", "材料"],
            [[row["symbol"], row["name"], "完整" if row["cache_target_complete"] else "部分",
              f"{row['expected_history']['expected_required_rows']}/{row['expected_history']['actual_raw_rows']}",
              "就绪" if row["strategy_inputs_ready"] else "有缺口", row["technical_status"], row["eligibility_status"], row["material_queue_status"]]
             for row in report["evaluations"]]]], ["paragraph", "320日为缓存目标，120个有效行情日、MA20/60、20日均成交额5000万元及相对基准对齐要求不变。停牌证据只解释应有范围，不制造行情或跳过关键窗口缺日。"]]])
        sections.append(["逐日期历史解释", [["table", [["证券", "有据非应有日线日期", "仍未解释的原价/复权日期", "固定策略有效日"],
            [[row["symbol"], "、".join(row["evidenced_non_trading_dates"]) or "无缺日",
              str(row["unexplained_raw_dates"])+" / "+str(row["unexplained_adjusted_dates"]), row["fixed_strategy_valid_days"]]
             for row in report["history_gap_accounting"]]]], ["paragraph", f"精确的BaoStock历史tradestatus证明与原新浪响应逐日期对齐。量额空字段保持未知，停牌占位OHLC没有写入价格库。原缓存{counts['cache_complete_count']}/{counts['stock_count']}完整，不能把状态解释写成新增320行。"]]])
        for row in report["evaluations"]:
            sections.append([row["symbol"]+" "+row["name"], [["paragraph", "行业关联："+"、".join(row["sector_ids"])+"；上市板块："+row["listing_board"]],
                ["table", [["确定性指标", "数值（展示四舍五入，JSON保留精确值）"], [[METRIC_LABELS.get(key, key), "无法计算" if value is None else str(value) if key == "valid_history_count" else number(value, percent="return_20" in key)] for key, value in sorted(row["metrics"].items())]]],
                ["table", [["技术/资格条件", "状态", "原因"], [[c["label"], c["status"], c["reason"]] for c in row["technical_conditions"]+row["eligibility_conditions"]]]],
                ["table", [["资格字段", "值", "目标日期", "源业务日期", "取得时间", "证据ID", "来源/原因"],
                    [[FIELD_LABELS.get(f["field"], f["field"]), "unknown" if f["value"] is None else str(f["value"]).lower(),
                      f["target_date"], "null" if f["source_business_date"] is None else f["source_business_date"],
                      f.get("fetched_at") or f.get("observed_at") or "null", f.get("evidence_id") or "未取得",
                      str(f.get("source_id"))+"；"+f["reason"]] for f in row["eligibility_facts"]]]],
                ["paragraph", "原价/复权来源版本："+json.dumps({key: value for key, value in row["source_versions"].items() if key != "raw_fact_hashes"}, ensure_ascii=False, sort_keys=True)],
                ["paragraph", "逐日原价内容哈希保存在JSON的source_versions.raw_fact_hashes，正文不重复展开。"]]])
        company_blocks = []
        for package in report["materials"]:
            company_blocks += [["subheading", package["symbol"]+" "+package["name"]+" · "+package["status"]],
                ["paragraph", "此对象技术与资格状态见上表；这些数值和资格事实不是公司主营或业务受益证据。"],
                ["paragraph", "现有本地正文索引检查："+str(package["search_record"]["scanned_index_rows"])+"条，匹配"+str(package["search_record"]["match_count"])+"条；新网页请求0。"],
                ["table", [["公司研究内容", "结果/缺口"], [["主营业务及业务构成", "尚无可定位正文依据"], ["相关公司公告", "尚无可用正文，标题不能代替正文"],
                  ["事项进展", "未知，不把计划写成完成"], ["反面信息", "覆盖未核验；未取得不等于没有风险"],
                  ["未解决问题", "；".join(package["unknowns"])]]]],
                ["paragraph", "原文、发布时间、实际取得时间、内容哈希与引用位置逐项见材料JSON；没有资料的字段不生成虚假证据ID。"],
                ["paragraph", "权限与下一步："+json.dumps(package["required_permissions"], ensure_ascii=False, sort_keys=True)]]
        if not company_blocks:
            company_blocks = [["paragraph", "无技术通过且未被资格排除的对象，公司材料队列为空；未替换证券凑数。"]]
        sections.append(["公司证据准备与未解决问题", company_blocks])
    sections.append(["后续条件与用途边界", [["paragraph", "本轮模型调用及Token为0。需要可定位的公司主营原文、相关公告/事项进度和反面证据检查，并核对来源及模型外发许可，才可讨论F4-S2。"],
        ["paragraph", "本地可读不自动允许外发模型。工程对象即使各条件通过，也不进入生产日报、今日机会、候选统计、模型队列或通知。未修改部署或定时任务。"]]])
    return sections


def build_report(selection, bundle, generated_at, report_id):
    """Pure presentation projection; qualification status comes from its engine."""
    verify_selection(selection)
    engineering = selection.get("purpose") == "engineering_validation"
    if not engineering and (selection["members"] or selection.get("selection_status") != "no_matching_sectors"
                            or not selection.get("industry_comparison_complete") or not selection.get("selection_verified")):
        raise ValueError("production_zero_report_requires_verified_empty_selection")
    rows, accounts = [], []
    if engineering:
        qualified = {row["security_id"]: row for row in bundle["eligibility"]["evaluations"]}
        readiness = {row["security_id"]: row for row in bundle["readiness"]}
        accounts = expected_history(bundle["readiness"])
        accounts_by_id = {row["security_id"]: row for row in accounts}
        for row in bundle["technical"]["evaluations"]:
            sid = row["security_id"]
            q, r = qualified[sid], readiness[sid]
            rows.append({**deepcopy(row), "eligibility_status": q["eligibility_status"], "eligibility_conditions": deepcopy(q["conditions"]),
                "eligibility_facts": deepcopy(q["facts"]), "eligibility_gaps": deepcopy(q["gaps"]),
                "material_queue_status": q["material_queue_status"], "exclusion_reasons": deepcopy(q["exclusion_reasons"]),
                "technical_source_risk_gaps": deepcopy(row.get("risk_gaps", [])), "risk_gaps": [gap["reason"] for gap in q["gaps"]],
                "history_window_accounted_for": r["history_window_accounted_for"], "expected_history": accounts_by_id[sid],
                "formal_candidate": False, "model_queue_eligible": False, "production_eligible": False})
    comparison = []
    if not engineering:
        for sector in selection["sectors"]:
            value = deepcopy(sector)
            value["reason_texts"] = [REASONS.get(reason, reason) for reason in sector["reasons"]]
            comparison.append(value)
        comparison.sort(key=lambda row: (row.get("ranking_change_pct") is None, -(row.get("ranking_change_pct") or 0), row["sector_id"]))
    counts = {"stock_count": len(rows), "catalog_count": selection.get("catalog_count"),
        "preselected_count": None if engineering else selection["preselected_count"],
        "selected_count": None if engineering else selection["selected_count"],
        **{f"technical_{state}_count": sum(row["technical_status"] == state for row in rows) for state in ("pass", "fail", "unknown", "not_applicable")},
        **{f"eligibility_{state}_count": sum(row["eligibility_status"] == state for row in rows) for state in ("pass", "fail", "pending")},
        "cache_complete_count": sum(row["cache_target_complete"] for row in rows),
        "strategy_ready_count": sum(row["strategy_inputs_ready"] for row in rows),
        "expected_required_rows": sum(row["expected_required_rows"] for row in accounts),
        "actual_raw_rows": sum(row["actual_raw_rows"] for row in accounts),
        "formal_candidate_count": 0, "model_queue_count": 0}
    report = {"schema_version": SCHEMA, "report_id": report_id, "report_kind": "engineering_diagnostic" if engineering else "production_daily",
        "purpose": "engineering_validation" if engineering else "production", "production_eligible": not engineering, "mode": selection["mode"],
        "title": "沪深 A 股·板块精选研究 — 独立工程报告" if engineering else "沪深 A 股·板块精选研究日报",
        "notice": "工程验证，不代表自然自动入选或已核验投资机会；暂不含北交所。" if engineering else "历史重建的生产零关注日报；暂不含北交所，不覆盖关注板块以外的个股机会。",
        "selection_id": selection["selection_id"], "selection_content_hash": selection["content_hash"], "trade_date": selection["target_date"],
        "actual_generated_at": generated_at, "source_cutoff_at": selection.get("source_cutoff_at", selection["cutoff_at"]),
        "historical_reconstruction": True, "market_scope": "sse_szse_a", "research_mode": "sector_first", "taxonomy": selection["taxonomy"],
        "universe_count": selection.get("universe_count"), "universe_source_business_dates": deepcopy(selection.get("universe_source_business_dates", [])),
        "parameters": deepcopy(selection.get("parameters", {})), "sector_comparison": comparison, "evaluations": rows,
        "history_gap_accounting": accounts, "materials": deepcopy(bundle["materials"]["packages"]) if engineering else [],
        "counts": counts, "company_materials_status": bundle["materials"]["status"] if engineering else "not_triggered",
        "gaps": sorted({gap for package in bundle["materials"]["packages"] for gap in package["unknowns"]}) if engineering else [],
        "conclusion": "工程全分母保留；技术、资格与公司证据分别判断，材料不足仍阻塞。" if engineering else "本分类体系下无行业满足当前观察规则。",
        "history_status": ("complete_cache" if counts["cache_complete_count"] == len(rows) else "partial_cache") if engineering else "not_triggered", "history_coverage_ratio": None,
        "eligibility_coverage_ratio": None if not rows else sum(row["eligibility_status"] != "pending" for row in rows)/len(rows),
        "model_calls": 0, "model_tokens": 0, "network_requests": 0, "database_writes": 0, "f4s2_ready": False}
    report["sections"] = _sections(report)
    report["content_hash"] = digest(report)
    validate_report(report, selection)
    return report



def read_published_report(root, directory, *, allow_engineering=False):
    root = Path(root).resolve()
    directory = local_path(root, directory)
    manifest = _json(directory/"manifest.json")
    if manifest.get("schema_version") != MANIFEST_SCHEMA or manifest.get("report_id") != directory.name or set(manifest.get("files", {})) != FILES:
        raise ValueError("sector_report_manifest_invalid")
    for name, expected in manifest["files"].items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest() != expected:
            raise ValueError("sector_report_artifact_hash_mismatch")
    selection, inputs = _json(directory/"sector_selection.json"), _json(directory/"report_inputs.json")
    report = validate_report(_json(directory/"sector_research_report.json"), selection)
    if report["purpose"] == "engineering_validation" and not allow_engineering:
        raise ValueError("engineering_report_requires_explicit_read")
    if report["purpose"] == "production" and (not is_production_path(directory) or not is_production_artifact(manifest)):
        raise ValueError("production_report_path_or_manifest_conflict")
    if (manifest.get("purpose") != report["purpose"] or manifest.get("production_eligible") != report["production_eligible"]
            or inputs.get("selection_content_hash") != selection["content_hash"] or inputs.get("purpose") != report["purpose"]):
        raise ValueError("sector_report_input_purpose_conflict")
    for ref in inputs["source_refs"]:
        if _file_ref(root, ref["path"])["sha256"] != ref["sha256"]:
            raise ValueError("sector_report_source_hash_mismatch")
    # Read an old report in its stored presentation version. Revalidate the
    # structured projection, not today's wording/order against yesterday's text.
    validate_report_inputs(report, selection, inputs)
    return report


def _summary(report, directory, reused=False):
    return {"status": "production_zero_report_published" if report["purpose"] == "production" else "engineering_report_published",
        "selection_id": report["selection_id"], "report_id": report["report_id"], "purpose": report["purpose"],
        "production_eligible": report["production_eligible"], "counts": report["counts"], "reused_report": reused,
        "output_directory": str(directory), "html_path": str(directory/"sector_research_report.html"),
        "markdown_path": str(directory/"sector_research_report.md"), "json_path": str(directory/"sector_research_report.json"),
        "model_calls": 0, "model_tokens": 0, "network_requests": 0, "database_writes": 0, "f4s2_ready": False}


def publish_sector_report(root, selection_id, *, revision=None, validation_config_path="config/sector_validation.json", dry_run=False, report_id=None):
    from ashare_daily.sector_workflow import _selection
    from ashare_daily.sector_pipeline import write_new, selection_directory
    from ashare_daily.sector_f4s1 import _bytes, _csv
    root = Path(root).resolve()
    selection, config = _selection(root, selection_id, validation_config_path)
    engineering = selection.get("purpose") == "engineering_validation"
    base = _base(root, selection, config)
    if dry_run:
        return {"status": "dry_run", "purpose": selection.get("purpose", "production"), "denominator": len(selection["members"]), "network_requests": 0, "model_calls": 0}
    if report_id:
        if not re.fullmatch(r"sector-report-[a-f0-9]{24}", report_id):
            raise ValueError("sector_report_id_invalid")
        report = read_published_report(root, base/report_id, allow_engineering=engineering)
        return _summary(report, base/report_id, reused=True)
    generated_at = datetime.now(SHANGHAI).isoformat()
    if engineering:
        from ashare_daily.sector_f4s1 import read_revision, readiness_rows, _company_basis
        from ashare_daily.sector_screening import evaluate_selection
        from ashare_daily.sector_eligibility import read_field_facts, evaluate_sector_eligibility
        from ashare_daily.sector_gap_diagnosis import read_gap_diagnosis
        from ashare_daily.sector_company_evidence import prepare_company_materials
        if not re.fullmatch(r"f4s1-[a-f0-9]{24}", revision or ""):
            raise ValueError("engineering_report_requires_explicit_f4s1_revision")
        source_directory = local_path(root, config["output_directory"])/selection_id/"f4s1/revisions"/revision
        read_revision(source_directory, selection, root=root)
        checkpoint = _json(source_directory/"checkpoint.json")
        gaps = read_gap_diagnosis(root, checkpoint["gap_path"], selection, config)
        facts = read_field_facts(root, checkpoint["facts_path"], selection)
        source_inputs, strategy = _json(source_directory/"screening_inputs.json"), _json(source_directory/"strategy_config.json")
        source_refs = _refs(root, [facts, gaps, source_inputs, *[_file_ref(root, source_directory/name) for name in (
            "manifest.json", "screening_inputs.json", "strategy_config.json", "checkpoint.json")],
            _file_ref(root, checkpoint["facts_path"]), _file_ref(root, checkpoint["gap_path"])])
        company_basis = _company_basis(root, selection)
    else:
        if revision or selection["members"] or selection.get("selection_status") != "no_matching_sectors":
            raise ValueError("production_report_requires_own_verified_empty_selection")
        source_directory = selection_directory(root, config, selection_id)
        source_refs = _refs(root, [_file_ref(root, source_directory/name) for name in ("sector_selection.json", "frozen_config.json", "source_inputs.json")])
        company_basis = None
    implementation = {name: hashlib.sha256((Path(__file__).parents[1]/name).read_bytes()).hexdigest() for name in (
        "reports/sector_research.py", "reports/sector_contracts.py", "reports/m3_render.py", "reports/templates/research.html", "sector_screening.py", "sector_eligibility.py", "sector_company_evidence.py")}
    fingerprint = digest({"selection_content_hash": selection["content_hash"], "source_refs": source_refs,
                          "company_basis": company_basis, "implementation": implementation})
    report_id = "sector-report-"+fingerprint[:24]
    directory = base/report_id
    if (directory/"manifest.json").exists():
        return _summary(read_published_report(root, directory, allow_engineering=engineering), directory, reused=True)
    if (directory/"report_inputs.json").exists():
        frozen = _json(directory/"report_inputs.json")
        if frozen["fingerprint"] != fingerprint:
            raise ValueError("interrupted_report_input_conflict")
        generated_at, bundle = frozen["actual_generated_at"], frozen["bundle"]
    elif engineering:
        technical = evaluate_selection(selection, source_inputs, strategy)
        eligibility = evaluate_sector_eligibility(selection, technical, facts, cutoff_at=generated_at)
        materials = prepare_company_materials(root, selection, technical, eligibility, cutoff_at=generated_at)
        bundle = {"technical": technical, "eligibility": eligibility, "materials": materials,
                  "readiness": readiness_rows(technical, eligibility, gaps), "gap_proof_hash": gaps["content_hash"]}
    else:
        bundle = {}
    report = build_report(selection, bundle, generated_at, report_id)
    inputs = {"schema_version": "f4s1-sector-report-input-v1", "selection_content_hash": selection["content_hash"], "purpose": report["purpose"],
        "production_eligible": report["production_eligible"], "fingerprint": fingerprint, "source_refs": source_refs,
        "company_basis": company_basis, "actual_generated_at": generated_at, "bundle": bundle, "implementation": implementation}
    hashes = {"report_inputs.json": write_new(directory/"report_inputs.json", inputs), "sector_selection.json": write_new(directory/"sector_selection.json", selection),
              "sector_research_report.json": write_new(directory/"sector_research_report.json", report)}
    hashes["sector_research_report.md"] = _bytes(directory/"sector_research_report.md", render_sections_markdown(report["title"], report["notice"], report["sections"]).encode())
    hashes["sector_research_report.html"] = _bytes(directory/"sector_research_report.html", render_sections_html(report["title"], report["trade_date"], report["notice"], report["sections"], eyebrow="F4-S1 · 只读证据整理 · 项目模型调用0").encode())
    hashes["eligibility_fields.csv"] = _csv(directory/"eligibility_fields.csv", [dict(fact, eligibility_status=row["eligibility_status"], symbol=row["symbol"])
        for row in report["evaluations"] for fact in row["eligibility_facts"]])
    hashes["history_gap_accounting.csv"] = _csv(directory/"history_gap_accounting.csv", report["history_gap_accounting"])
    write_new(directory/"manifest.json", {"schema_version": MANIFEST_SCHEMA, "report_id": report_id, "selection_id": selection_id,
        "selection_content_hash": selection["content_hash"], "purpose": report["purpose"], "production_eligible": report["production_eligible"],
        "market_scope": "sse_szse_a", "research_mode": "sector_first", "fingerprint": fingerprint, "files": hashes})
    return _summary(read_published_report(root, directory, allow_engineering=engineering), directory)
