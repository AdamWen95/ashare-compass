"""Local, immutable daily price/volume observations and bounded read-only reader."""
from __future__ import annotations

import csv
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
from uuid import uuid4

from .m3_render import render_sections_html, render_sections_markdown
from .m2_render import _csv_cell

SCHEMA = "daily-observation-report-v1"
EXPORTS = {
    "daily_observation.md": ("观察日报 Markdown", "text/markdown"),
    "daily_observation.html": ("观察日报 HTML", "text/html"),
    "daily_observation.json": ("观察日报 JSON", "application/json"),
    "screening_audit.csv": ("全部关注股票筛选明细", "text/csv"),
}
FILES = set(EXPORTS) | {"report_inputs.json"}
RELATIVE_ROOT = "research/sse_szse_a/observation_reports"
NOTICE = "量价观察名单，供进一步研究；公司公告与基本面未完成核查，不代表没有风险。不含交易指令，不保证每天出现达标股票。"
RECOMMENDATION_VERSION = "daily-research-candidates-v1"
RECOMMENDATION_NOTICE = "A股研究候选由量价与基本资格规则筛选，重点关注名单沿用原排序；部分财务数据仅补充研究事实。公司公告、主营业务与反面证据仍待核查，不含交易指令，不保证每天出现达标股票。"


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+"\n").encode()


def sha(body):
    return hashlib.sha256(body).hexdigest()


def _number(value, percent=False):
    if value is None:
        return "未取得"
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite_observation_metric")
    return f"{number * 100:.2f}%" if percent else f"{number:,.2f}"


def _eligibility_policy(packet):
    """Missing policy preserves the contract of previously frozen reports."""
    if "eligibility_policy" not in packet:
        return None
    policy = packet["eligibility_policy"]
    if (not isinstance(policy, dict) or set(policy) != {"require_delisting_check"}
            or type(policy["require_delisting_check"]) is not bool):
        raise ValueError("observation_eligibility_policy_invalid")
    return policy


def eligibility_scope_notice(report):
    policy = _eligibility_policy(report)
    if policy is None:
        return None
    scope = ("本期基本资格筛选包含退市整理期状态。" if policy["require_delisting_check"]
        else "本期基本资格筛选不含退市整理期；仍核验证券身份、上市状态、ST和停牌条件。")
    return scope + "资格通过仅表示本期所列条件通过，不代表已完成全部风险核查。"


def build_report(selection, observation, context, *, planned_cutoff, generated_at, financial_review=None,
                 reference_review=None):
    from ..sector_selection import digest, verify_selection
    verify_selection(selection)
    if (selection.get("mode") != "research" or selection.get("purpose", "production") != "production" or selection.get("production_eligible") is False
            or selection.get("selection_verified") is not True or selection.get("research_mode") != "sector_first"
            or selection.get("market_scope") != "sse_szse_a" or selection["selection_id"].startswith("validation-")):
        raise ValueError("observation_requires_verified_production_selection")
    if observation.get("selection_id") != selection["selection_id"] or observation.get("target_date") != selection["target_date"]:
        raise ValueError("observation_selection_binding_mismatch")
    if (observation.get("purpose") != "production" or observation.get("production_eligible") is not True
            or observation.get("mode") != "research" or observation.get("selection_content_hash") != selection["content_hash"]
            or observation.get("content_hash") != digest({k: v for k, v in observation.items() if k != "content_hash"})):
        raise ValueError("observation_provenance_or_content_invalid")
    technical = observation.get("technical", {})
    eligibility = observation.get("eligibility", {})
    policy = _eligibility_policy(observation)
    if policy != _eligibility_policy(eligibility):
        raise ValueError("observation_eligibility_policy_binding_mismatch")
    if (technical.get("result_hash") != digest({k: v for k, v in technical.items() if k != "result_hash"})
            or eligibility.get("content_hash") != digest({k: v for k, v in eligibility.items() if k != "content_hash"})
            or eligibility.get("technical_result_hash") != technical.get("result_hash")):
        raise ValueError("observation_axes_content_invalid")
    for packet in (technical, eligibility):
        if any(packet.get(k) != selection.get(k) for k in ("selection_id", "target_date", "mode")) or packet.get("purpose") != "production":
            raise ValueError("observation_axes_scope_invalid")
    qualified = {row["security_id"]: row for row in observation.get("eligibility", {}).get("evaluations", [])}
    rows = []
    for row in technical.get("evaluations", []):
        qualification = qualified.get(row["security_id"], {})
        status = qualification.get("eligibility_status", "pending")
        rows.append({key: row.get(key) for key in ("security_id", "symbol", "name", "metrics", "technical_status", "technical_conditions",
            "strategy_inputs_ready", "cache_target_complete", "risk_gaps")})
        rows[-1].update(eligibility_status=status, eligibility_conditions=qualification.get("conditions", []),
            eligibility_gaps=qualification.get("gaps", []), exclusion_reasons=qualification.get("exclusion_reasons", []),
            company_materials_status="not_reviewed", company_risk_notice="公司公告、经营财务及反面证据尚未核查",
            eligibility_evidence_ids=sorted({fact["evidence_id"] for fact in qualification.get("facts", []) if fact.get("evidence_id")}))
    if ({r["security_id"] for r in rows} != {m["security_id"] for m in selection["members"]}
            or len(rows) != len(selection["members"])):
        raise ValueError("observation_member_denominator_mismatch")
    ranked = sorted((r for r in rows if r["technical_status"] == "pass" and r["eligibility_status"] == "pass"),
        key=lambda r: (-Decimal(r["metrics"]["relative_return_20"]), -Decimal(r["metrics"]["avg_amount_20_cny"]), r["symbol"]))
    pending = [r for r in rows if r["technical_status"] == "pass" and r["eligibility_status"] == "pending"]
    limit = min(20, technical["strategy_config"]["max_candidates"])
    counts = {"stock_count": len(rows), "observation_count": min(len(ranked), limit), "qualified_count": len(ranked),
        "pending_count": len(pending), "technical_pass_count": sum(r["technical_status"] == "pass" for r in rows),
        "technical_unknown_count": sum(r["technical_status"] == "unknown" for r in rows),
        "eligibility_fail_count": sum(r["eligibility_status"] == "fail" for r in rows)}
    report = {"schema_version": SCHEMA, "purpose": "production", "production_eligible": True,
        "mode": selection["mode"], "market_scope": "sse_szse_a", "research_mode": "sector_first",
        "selection_id": selection["selection_id"], "selection_content_hash": selection["content_hash"],
        "trade_date": selection["target_date"], "planned_cutoff_at": planned_cutoff,
        "observation_cutoff_at": observation.get("cutoff_at", selection["cutoff_at"]),
        "actual_generated_at": generated_at, "historical_reconstruction": bool(selection.get("historical_reconstruction"))
            or generated_at[:10] != selection["target_date"],
        "title": "今日方向简报 · 量价观察名单", "notice": NOTICE, "counts": counts,
        "observations": ranked[:limit], "pending": pending, "evaluations": rows,
        "universe_count": selection.get("universe_count"), "sector_comparison": selection.get("sectors", []),
        "selected_sector_count": selection.get("selected_count", sum(bool(s.get("selected")) for s in selection.get("sectors", []))),
        "selection_status": selection.get("selection_status"), "data_gaps": observation.get("gaps", []),
        "data_status": observation.get("status"), "model_context": context,
        "model_run": context.get("model_run", {"status": context.get("status", "not_run"), "call_count": 0}),
        "source_result_hash": observation.get("content_hash"),
        "ranking_basis": f"按20日相对上证收益降序、20日平均成交额降序；最多展示{limit}只，完整结果保留于明细",
        "company_materials_gate": "optional_for_technical_observation_only"}
    if policy is not None:
        report["eligibility_policy"] = deepcopy(policy)
    if financial_review is not None:
        from ..financial_review import validate_financial_review
        validate_financial_review(financial_review, candidates=report["observations"][:5],
            target_date=report["trade_date"], cutoff_at=report["planned_cutoff_at"])
        report.update(recommendation_version=RECOMMENDATION_VERSION,
            title="今日方向简报 · A股研究候选", notice=RECOMMENDATION_NOTICE,
            financial_review=deepcopy(financial_review), strategy_config=deepcopy(technical["strategy_config"]),
            strategy_config_hash=digest(technical["strategy_config"]))
        records = {r["security_id"]: r for r in financial_review["records"]}
        for row in rows:
            row.update(_company_review_fields(records.get(row["security_id"])))
        report["focus_candidates"] = ranked[:min(5, limit)]
    if reference_review is not None:
        report["reference_review"] = deepcopy(reference_review)
        _check_reference_review(report)
    report["sections"] = sections(report)
    return report


def _company_review_fields(record):
    if record and (record.get("profit") or record.get("cash_flow")):
        return {"company_materials_status": "partial_financial_review",
            "company_risk_notice": "已取得部分财务数据；公司公告、主营业务及反面证据仍未完成核查"}
    return {"company_materials_status": "not_reviewed",
        "company_risk_notice": "财务数据未齐；公司公告、主营业务及反面证据仍未完成核查"}


def _selection_basis(strategy):
    """Describe the frozen rule parameters without adding investment judgments."""
    required = ("min_history_trading_days", "amount_days", "min_avg_amount_cny", "ma_short_days",
        "ma_long_days", "return_days", "benchmark_name")
    if any(key not in strategy for key in required):
        return "入选依据：本期冻结规则中的量价条件与基本资格均通过；完整条件见筛选明细。"
    return (f"入选依据：有效行情满足{strategy['min_history_trading_days']}个交易日；"
        f"近{strategy['amount_days']}日平均成交额不少于{_number(strategy['min_avg_amount_cny'])}元；"
        f"前复权收盘价高于MA{strategy['ma_short_days']}，MA{strategy['ma_short_days']}高于MA{strategy['ma_long_days']}；"
        f"近{strategy['return_days']}日表现强于{strategy['benchmark_name']}；基本资格通过。")


def _financial_blocks(record):
    if record is None:
        return [("paragraph", "财务资料：本次未取得；未取得不表示没有财务风险。")]
    labels = {"available": "本期两类财务接口有返回", "partial": "仅取得部分财务资料", "unavailable": "未取得可用财务资料"}
    blocks = [("paragraph", "财务资料：" + labels.get(record["status"], "未取得可用财务资料")
        + "。以下保留来源原始值；比例单位及累计/单季口径未核实，不换算百分比，不参与排序。")]
    raw_rows = []
    for operation, title, fields in (
        ("profit", "盈利能力", (("netProfit", "净利润"), ("roeAvg", "净资产收益率"),
            ("npMargin", "净利率"), ("gpMargin", "毛利率"), ("epsTTM", "每股收益TTM"), ("MBRevenue", "主营营业收入"))),
        ("cash_flow", "现金流量比率", (("CFOToOR", "经营现金流/营业收入"),
            ("CFOToNP", "经营现金流/净利润"), ("CFOToGr", "经营现金流/营业总收入"))),
    ):
        source = record.get(operation)
        if not source:
            continue
        provenance = record["provenance"][operation]
        blocks.append(("paragraph", f"{title}：报告期 {source['statDate']}；发布日期 {source['pubDate']}；"
            f"首次取得 {provenance['first_observed_at']}；本版本观察时间 {provenance['observed_at']}；来源 BaoStock。"
            + ("历史补采资料，不能视为当时已掌握。" if provenance.get("historical_reconstruction") else "")))
        raw_rows.extend([[title, label, source.get(field) or "未取得", field]
            for field, label in fields])
    if raw_rows:
        blocks.append(("table", (["数据类别", "字段含义", "来源原始值（口径待核实）", "来源字段"], raw_rows)))
    blocks.extend(("paragraph", "财务风险事实：" + str(flag)) for flag in record.get("risk_flags", []))
    blocks.extend(("paragraph", "财务资料缺口：" + str(gap)) for gap in record.get("gaps", []))
    if not record.get("risk_flags"):
        blocks.append(("paragraph", "本次返回字段未触发已定义的财务风险标记，不代表公司没有其他财务风险。"))
    return blocks


def _focus_section(report):
    packet = report["financial_review"]
    records = {record["security_id"]: record for record in packet["records"]}
    blocks = [("paragraph", "重点研究前5只沿用本期量价与基本资格达标名单的顺序；财务资料不改变原筛选门槛和排名。")]
    if not report["focus_candidates"]:
        blocks.append(("paragraph", "本期没有同时满足量价及基本资格条件的研究候选，保留空名单。"))
    else:
        labels = {"available": "两类财务接口有返回", "partial": "部分财务资料", "unavailable": "财务待查"}
        blocks.append(("table", (["研究顺序", "证券", "名称", "财务覆盖", "待核查事项"], [
            [rank, row["symbol"], row["name"], labels.get(records.get(row["security_id"], {}).get("status"), "财务待查"),
             "；".join(records.get(row["security_id"], {}).get("risk_flags", []) + ["公司公告、主营业务及反面证据待查"])]
            for rank, row in enumerate(report["focus_candidates"], 1)])))
    blocks.append(("paragraph", f"财务采集状态：{packet['status']}；本次请求 {packet['network_requests']} 次，缓存复用 {packet['cache_hits']} 项。"))
    blocks.extend(("paragraph", "覆盖限制：" + str(value)) for value in packet.get("limitations", []))
    return "重点研究候选", blocks


def _check_reference_review(report):
    from ..reference_strategy import validate_reference_review
    packet = report["reference_review"]
    validate_reference_review(packet, evaluations=report["evaluations"], trade_date=report["trade_date"],
        source_observation_hash=report["source_result_hash"])
    if packet.get("selection_id") != report["selection_id"]:
        raise ValueError("reference_review_selection_mismatch")


def _reference_section(report):
    packet = report["reference_review"]
    records = {row["security_id"]: row for row in packet["records"]}
    blocks = [("paragraph", "参考策略技术辅助评分，试运行；不改变正式候选资格或排序；并非成功概率，尚未证明优于原策略。"),
        ("paragraph", "各项指标由本地程序在冻结行情上计算，原有基本资格与数据完整性门槛继续生效。")]
    rows = [records[row["security_id"]] for row in report["observations"][:20]]
    pending_only = not rows
    if pending_only:
        rows = sorted((row for row in records.values() if row["baseline_technical_status"] == "pass"
            and row["eligibility_status"] == "pending" and row["status"] == "available"),
            key=lambda row: (-row["score"], row["symbol"], row["security_id"]))[:5]
        blocks.append(("paragraph", "本期没有正式候选；以下仅展示辅助观察前5只，资格待查，不是正式推荐。" if rows
            else "本期没有可展示的正式候选或资格待查辅助观察，保留空名单。"))
    else:
        blocks.append(("paragraph", "以下顺序与正式候选保持一致，辅助分数不改变原排序。"))
    if rows:
        blocks.append(("table", (["展示顺序", "证券", "名称", "辅助分数", "资格状态"], [
            [rank, row["symbol"], row["name"], row["score"] if row["status"] == "available" else "未取得",
             "资格待查，不是正式推荐" if pending_only else "原规则资格通过"]
            for rank, row in enumerate(rows, 1)])))
    for row in rows:
        blocks.append(("subheading", f"{row['name']}（{row['symbol']}）辅助评分依据"))
        if row["status"] == "available":
            blocks.append(("paragraph", f"固定行情窗口：{row['window_start']} 至 {row['window_end']}。"))
            blocks.append(("table", (["技术项目", "加减分", "计算依据"], [
                [component["label"], f"{component['points']:+d}", component["reason"]]
                for component in row["components"]])))
        blocks.extend(("paragraph", "评分数据缺口：" + str(issue)) for issue in row.get("issues", []))
    blocks.extend(("paragraph", "辅助策略限制：" + str(value)) for value in packet.get("limitations", []))
    blocks.append(("paragraph", "参考源码版本：https://github.com/ktoking/Intelligent-stock-selector/tree/" + packet["upstream_commit"]))
    return "参考策略技术辅助评分（试运行）", blocks


def sections(report):
    enhanced = "financial_review" in report
    counts = report["counts"]
    overview = [("table", (["项目", "实际记录"], [
        ["行情日期", report["trade_date"]], ["计划资料截点", report["planned_cutoff_at"]],
        ["行情/资格实际观察截点", report["observation_cutoff_at"]], ["生成时间", report["actual_generated_at"]],
        ["名单发现范围", report["universe_count"]], ["入选行业内股票数", counts["stock_count"]],
        ["量价及资格达标 / 资格待查", f"{counts['qualified_count']} / {counts['pending_count']}"],
        ["历史重建", "是，保留实际取得时间" if report["historical_reconstruction"] else "否"],
    ])), ("paragraph", "范围为沪深普通A股，暂不含北交所；先按19个粗行业门类筛选，再分析入选行业内股票。")]
    policy_notice = eligibility_scope_notice(report)
    if policy_notice:
        overview.append(("paragraph", policy_notice))
    watch = [("paragraph", report["ranking_basis"])]
    if not report["observations"]:
        reason = "本分类体系下无行业满足当前观察规则。" if not counts["stock_count"] else "本期尚无量价和基本资格同时通过的股票；请结合数据缺口与待查表阅读。"
        watch.append(("paragraph", reason))
    for rank, row in enumerate(report["observations"], 1):
        metrics = row["metrics"]
        watch += [("subheading", f"{rank}. {row['name']}（{row['symbol']}）"),
            ("paragraph", _selection_basis(report["strategy_config"]) if enhanced else "入选依据：有效行情满足120日；20日均成交额达标；前复权收盘价高于MA20、MA20高于MA60；20日表现强于上证综指；基本资格通过。"),
            ("table", (["本地计算指标", "值"], [["前复权收盘", _number(metrics.get("adjusted_close"))],
                ["MA20 / MA60", f"{_number(metrics.get('ma20'))} / {_number(metrics.get('ma60'))}"],
                ["20日平均成交额（元）", _number(metrics.get("avg_amount_20_cny"))],
                ["20日相对上证收益", _number(metrics.get("relative_return_20"), True)]])),
            ("paragraph", "待核查与风险：" + row["company_risk_notice"] + "；量价条件反映过去表现，后续可能变化。")]
        if enhanced:
            watch.append(("paragraph", "后续观察条件：下一交易日继续核验趋势、相对基准表现、成交活跃度及基本资格；任何条件失效或证据缺失，后续报告按原规则重新判断。"))
            watch.append(("paragraph", "反面情形：近期相对强势可能回落，行业方向可能变化；尚未核查的公告或经营变化可能削弱当前研究依据。"))
            if rank <= 5:
                record = next((item for item in report["financial_review"]["records"] if item["security_id"] == row["security_id"]), None)
                watch.extend(_financial_blocks(record))
    pending = [("paragraph", "以下仅量价达标，基本资格证据尚不齐，不列入达标观察名单。"),
        ("table", (["证券", "名称", "待核查字段"], [[r["symbol"], r["name"],
            json.dumps(r["eligibility_gaps"], ensure_ascii=False)] for r in report["pending"]]))]
    context = report["model_context"]
    news = [("paragraph", "模型仅处理获准外发的新闻资料；没有接收行情、个股指标或候选名单，也不负责排序。"),
        ("paragraph", "模型状态：" + str(context.get("status", "not_run")))]
    for claim in context.get("analysis", {}).get("accepted_claims", []):
        labels = {"fact": "资料事实", "inference": "研究推论，待人工复核", "opinion": "来源观点",
            "counterevidence": "反面证据", "unknown": "尚未确认", "followup": "后续核查"}
        news.append(("paragraph", labels.get(claim.get("claim_type"), "资料主张") + "：" + str(claim.get("text", ""))))
        for citation in claim.get("citations", []):
            item = next((item for item in context.get("evidence_catalog", []) if item.get("evidence_id") == citation.get("evidence_id")), {})
            label = "历史背景" if item.get("is_background") else "区间内材料"
            news.append(("paragraph", f"证据 {citation.get('evidence_id')} · {label} · 发布时间 {item.get('published_at', '未知')} · {citation.get('locator')} · {citation.get('quote')}"))
    news.append(("paragraph", "新闻背景不自动证明上述公司受益；引用结构通过仍需结合原文判断。"))
    audit = [("table", (["证券", "名称", "技术", "资格", "公司材料"], [[r["symbol"], r["name"],
        r["technical_status"], r["eligibility_status"], "部分财务资料，公告待查" if enhanced and r["company_materials_status"] == "partial_financial_review" else "未核查"] for r in report["evaluations"]])),
        ("paragraph", f"存在行情或资格缺口的股票：{len(report['data_gaps'])}只；逐日缺口、来源响应和资格证明保存在本次任务的冻结输入中。"),
        ("paragraph", ("本期纳入资格条件的已确认风险继续排除；未知数据不补造，不以工程样本填补空名单。"
            if report.get("eligibility_policy", {}).get("require_delisting_check") is False
            else "已确认风险继续排除；未知数据不补造，不以工程样本填补空名单。"))]
    if enhanced:
        overview.append(("paragraph", "财务资料截点：" + report["financial_review"]["cutoff_at"]
            + "；首次取得时间单独保留，历史补采不冒充当时已知。"))
    return [("本期范围与数据日期", overview)] + ([_focus_section(report)] if enhanced else []) + [
            ("本期A股研究候选" if enhanced else "量价达标观察名单", watch)] + (
            [_reference_section(report)] if "reference_review" in report else []) + [("资格待查", pending),
            ("新闻与模型研究背景", news), ("完整筛选明细与缺口", audit)]


def _check_report(report):
    if (report.get("schema_version") != SCHEMA or report.get("purpose") != "production" or report.get("mode") != "research"
            or report.get("production_eligible") is not True or report.get("market_scope") != "sse_szse_a"
            or report.get("research_mode") != "sector_first" or not str(report.get("selection_id", "")).startswith("sector-")):
        raise ValueError("observation_report_scope_invalid")
    date.fromisoformat(report["trade_date"])
    for key in ("actual_generated_at", "planned_cutoff_at", "observation_cutoff_at"):
        if datetime.fromisoformat(report[key]).utcoffset() is None:
            raise ValueError("observation_time_requires_timezone")
    evaluations = report["evaluations"]
    policy = _eligibility_policy(report)
    if policy is not None:
        for row in evaluations:
            conditions = [c for c in row["eligibility_conditions"] if c.get("id") == "not_delisting_period"]
            if len(conditions) != 1:
                raise ValueError("observation_eligibility_policy_condition_missing")
            condition = conditions[0]
            if policy["require_delisting_check"]:
                if condition.get("required") is not True or condition.get("status") == "not_required":
                    raise ValueError("observation_eligibility_policy_condition_mismatch")
            elif (condition.get("required") is not False or condition.get("status") != "not_required"
                    or condition.get("evidence_status") not in {"pass", "fail", "unknown"}):
                raise ValueError("observation_eligibility_policy_condition_mismatch")
    if len({r["security_id"] for r in evaluations}) != len(evaluations) or len(evaluations) != report["counts"]["stock_count"]:
        raise ValueError("observation_report_denominator_invalid")
    permitted = {r["security_id"]: r for r in evaluations if r["technical_status"] == r["eligibility_status"] == "pass"}
    if any(r != permitted.get(r["security_id"]) for r in report["observations"]):
        raise ValueError("unqualified_observation_rejected")
    if len(report["observations"]) != report["counts"]["observation_count"] or len(permitted) != report["counts"]["qualified_count"]:
        raise ValueError("observation_counts_invalid")
    if "financial_review" in report:
        from ..financial_review import validate_financial_review
        from ..sector_selection import digest
        if (report.get("recommendation_version") != RECOMMENDATION_VERSION
                or report.get("title") != "今日方向简报 · A股研究候选" or report.get("notice") != RECOMMENDATION_NOTICE
                or report.get("strategy_config_hash") != digest(report.get("strategy_config", {}))):
            raise ValueError("recommendation_contract_invalid")
        ranked = sorted(permitted.values(), key=lambda row: (-Decimal(row["metrics"]["relative_return_20"]),
            -Decimal(row["metrics"]["avg_amount_20_cny"]), row["symbol"]))
        if (report["observations"] != ranked[:min(20, report["strategy_config"]["max_candidates"])]
                or report.get("focus_candidates") != report["observations"][:5]):
            raise ValueError("recommendation_focus_or_ranking_invalid")
        validate_financial_review(report["financial_review"], candidates=report["observations"][:5],
            target_date=report["trade_date"], cutoff_at=report["planned_cutoff_at"])
        records = {row["security_id"]: row for row in report["financial_review"]["records"]}
        if any(any(row.get(key) != value for key, value in _company_review_fields(records.get(row["security_id"])).items())
                for row in evaluations):
            raise ValueError("recommendation_company_review_overstated")
    elif any(key in report for key in ("recommendation_version", "focus_candidates", "strategy_config_hash")):
        raise ValueError("recommendation_financial_packet_missing")
    if "reference_review" in report:
        _check_reference_review(report)
        ranked = sorted(permitted.values(), key=lambda row: (-Decimal(row["metrics"]["relative_return_20"]),
            -Decimal(row["metrics"]["avg_amount_20_cny"]), row["symbol"]))
        if report["observations"] != ranked[:20]:
            raise ValueError("reference_review_changed_baseline_ranking")
    if report["sections"] != json.loads(json.dumps(sections(report), ensure_ascii=False)):
        raise ValueError("observation_display_content_mismatch")


def publish_observation(project, selection, observation, context, *, planned_cutoff, generated_at, financial_review=None,
                        reference_review=None):
    project = Path(project).resolve()
    report = build_report(selection, observation, context, planned_cutoff=planned_cutoff, generated_at=generated_at,
        financial_review=financial_review, reference_review=reference_review)
    report = json.loads(encoded(report))
    _check_report(report)
    identifier = "observation-" + sha(encoded(report))[:24]
    report["report_id"] = identifier
    inputs = {"purpose": "production", "production_eligible": True, "selection_id": selection["selection_id"],
        "selection_content_hash": selection["content_hash"], "member_ids": sorted(m["security_id"] for m in selection["members"]),
        "source_result_hash": observation.get("content_hash"), "model_input_snapshot": context.get("input_snapshot", {})}
    if "eligibility_policy" in report:
        inputs["eligibility_policy"] = deepcopy(report["eligibility_policy"])
    if financial_review is not None:
        inputs.update(financial_review=deepcopy(report["financial_review"]), strategy_config=deepcopy(report["strategy_config"]),
            strategy_config_hash=report["strategy_config_hash"], focus_security_ids=[r["security_id"] for r in report["focus_candidates"]])
    if reference_review is not None:
        inputs["reference_review"] = deepcopy(report["reference_review"])
    root = project / "outputs" / RELATIVE_ROOT
    directory = root / report["trade_date"] / identifier
    for parent in (directory, *directory.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("observation_publish_link_rejected")
        if parent == project:
            break
    if directory.exists():
        read_observation_report(project / "outputs", directory)
        return {"directory": str(directory), "report_id": identifier, "reused": True}
    stage = directory.with_name(".stage-" + identifier + "-" + uuid4().hex[:8])
    stage.mkdir(parents=True)
    content = {"daily_observation.json": encoded(report), "report_inputs.json": encoded(inputs),
        "daily_observation.md": render_sections_markdown(report["title"], report["notice"], report["sections"]).encode(),
        "daily_observation.html": render_sections_html(report["title"], report["trade_date"], report["notice"], report["sections"],
            eyebrow="盘后研究 · 本地量价筛选 · 可选新闻解释").encode()}
    stream = io.StringIO(newline="")
    fields = ["symbol", "name", "technical_status", "eligibility_status", "metrics", "technical_conditions", "eligibility_gaps", "company_risk_notice"]
    if "eligibility_policy" in report:
        fields.append("eligibility_conditions")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in report["evaluations"]:
        writer.writerow({k: _csv_cell(json.dumps(row[k], ensure_ascii=False) if isinstance(row[k], (dict, list)) else row[k]) for k in fields})
    content["screening_audit.csv"] = stream.getvalue().encode("utf-8-sig")
    for name, body in content.items():
        (stage / name).write_bytes(body)
    (stage / "manifest.json").write_bytes(encoded({"schema_version": SCHEMA, "report_id": identifier,
        "purpose": "production", "production_eligible": True, "files": {k: sha(v) for k, v in content.items()}}))
    from ..operations.backup import _publish_restored_stage
    _publish_restored_stage(stage, directory)
    read_observation_report(project / "outputs", directory)
    return {"directory": str(directory), "report_id": identifier, "reused": False}


def read_observation_report(output_root, directory):
    output_root, directory = Path(output_root).absolute(), Path(directory).absolute()
    base = output_root / RELATIVE_ROOT
    if directory.parent.parent != base or not re.fullmatch(r"observation-[a-f0-9]{24}", directory.name):
        raise ValueError("observation_report_path_invalid")
    for path in (directory, *directory.parents):
        if path.is_symlink() or path.is_junction():
            raise ValueError("observation_report_link_rejected")
        if path == output_root:
            break
    def read(name, limit=25_000_000):
        p = directory / name
        if p.is_symlink() or not p.is_file() or p.stat().st_size > limit:
            raise ValueError("observation_report_file_invalid")
        return p.read_bytes()
    manifest = json.loads(read("manifest.json", 100_000))
    if (manifest.get("schema_version") != SCHEMA or manifest.get("report_id") != directory.name
            or manifest.get("purpose") != "production" or manifest.get("production_eligible") is not True
            or set(manifest.get("files", {})) != FILES):
        raise ValueError("observation_manifest_invalid")
    content = {name: read(name) for name in FILES}
    if any(sha(body) != manifest["files"][name] for name, body in content.items()):
        raise ValueError("observation_artifact_hash_mismatch")
    report = json.loads(content["daily_observation.json"])
    _check_report(report)
    if directory.name != "observation-" + sha(encoded({k: v for k, v in report.items() if k != "report_id"}))[:24]:
        raise ValueError("observation_report_identity_mismatch")
    inputs = json.loads(content["report_inputs.json"])
    if (report["report_id"] != directory.name or report["trade_date"] != directory.parent.name
            or inputs.get("purpose") != "production" or inputs.get("production_eligible") is not True
            or inputs.get("selection_id") != report["selection_id"]
            or inputs.get("selection_content_hash") != report["selection_content_hash"]
            or sorted(r["security_id"] for r in report["evaluations"]) != inputs.get("member_ids")):
        raise ValueError("observation_inputs_binding_mismatch")
    if _eligibility_policy(inputs) != _eligibility_policy(report):
        raise ValueError("observation_eligibility_policy_inputs_mismatch")
    if "financial_review" in report:
        if (any(inputs.get(key) != report[key] for key in ("financial_review", "strategy_config", "strategy_config_hash"))
                or inputs.get("focus_security_ids") != [row["security_id"] for row in report["focus_candidates"]]
                or inputs.get("source_result_hash") != report.get("source_result_hash")
                or directory.name != "observation-" + sha(encoded({k: v for k, v in report.items() if k != "report_id"}))[:24]):
            raise ValueError("recommendation_frozen_inputs_mismatch")
    elif any(key in inputs for key in ("financial_review", "focus_security_ids", "strategy_config_hash")):
        raise ValueError("recommendation_report_packet_missing")
    if "reference_review" in report:
        if (inputs.get("reference_review") != report["reference_review"]
                or inputs.get("source_result_hash") != report.get("source_result_hash")
                or directory.name != "observation-" + sha(encoded({k: v for k, v in report.items() if k != "report_id"}))[:24]):
            raise ValueError("reference_review_frozen_inputs_mismatch")
    elif "reference_review" in inputs:
        raise ValueError("reference_review_report_packet_missing")
    return {"report": report, "directory": directory, "files": {k: content[k] for k in EXPORTS}}


def scan_observation_reports(output_root):
    base = Path(output_root) / RELATIVE_ROOT
    reports, problems = [], []
    if base.is_symlink() or base.is_junction():
        return [], [{"问题": "观察报告目录不能为链接"}]
    for day in sorted(base.glob("????-??-??"), reverse=True):
        if day.is_symlink() or day.is_junction():
            continue
        for directory in day.glob("observation-*"):
            try:
                reports.append(read_observation_report(output_root, directory))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                problems.append({"报告": directory.name, "问题": type(exc).__name__})
    reports.sort(key=lambda a: (a["report"]["trade_date"], a["report"]["actual_generated_at"]), reverse=True)
    return reports, problems
