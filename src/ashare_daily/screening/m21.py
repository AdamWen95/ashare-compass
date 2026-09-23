"""M2.1 separates numerical screening from evidenced eligibility; M2 is unchanged."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Literal

from ashare_daily.screening.engine import digest, evaluate_snapshot
from ashare_daily.screening.settings import StrategyConfig


class M21Config(StrategyConfig):
    workflow_version: Literal["m2.1"] = "m2.1"
    status_semantics_version: Literal["eligibility-split-v1"] = "eligibility-split-v1"
    sample_source: Literal["m1_config", "fixed_sample_config"] = "m1_config"
    sample_file: str | None = None
    eligibility_evidence_file: str | None = None

    def calculation_config(self) -> StrategyConfig:
        values = {key: value for key, value in self.model_dump(mode="json").items() if key in StrategyConfig.model_fields}
        # The old engine receives symbols from the frozen input, never from this
        # descriptive field. Keep its schema intact while retaining the true M2.1
        # selection source in the report and immutable snapshot.
        values["sample_source"] = "m1_config"
        return StrategyConfig.model_validate(values)


def load_m21_config(path: Path | str) -> M21Config:
    return M21Config.model_validate(json.loads(Path(path).read_text(encoding="utf-8-sig")))


TECHNICAL_IDS = {"history", "liquidity", "trend", "relative_strength", "data_integrity"}
ELIGIBILITY_IDS = {"identity", "listed", "not_st", "not_suspended", "not_delisting_period"}
NOTICE = "扩展样本试运行版，仅基于量价规则；新闻、公告和研报综合核查未完成"


def _state_conflicts(snapshot: dict, symbol: str) -> dict[str, list[str]]:
    """A raw/adjusted state conflict cannot be resolved by preferring raw bars.

    Compare frozen T rows directly as well as retaining the inconsistency notes
    already emitted by freeze_input. Missing adjusted data does not manufacture
    an opposite state; the ordinary technical data-gap checks still apply.
    """
    target = snapshot["trade_date"]
    raw_rows = [bar for bar in snapshot.get("raw_bars", []) if bar.get("symbol") == symbol and bar.get("trade_date") == target]
    adjusted_rows = [bar for bar in snapshot.get("adjusted_data", {}).get("series", {}).get(symbol, {}).get("bars", []) if bar.get("trade_date") == target]
    conflicts = {}
    for field, condition_id in (("is_st", "not_st"), ("tradestatus", "not_suspended")):
        reasons = [reason for reason in snapshot.get("source_issues", {}).get(symbol, [])
                   if field in reason and ("不一致" in reason or "冲突" in reason)
                   and (not re.match(r"^\d{4}-\d{2}-\d{2}", reason) or reason[:10] == target)]
        values = [bar.get(field) for bar in raw_rows + adjusted_rows if isinstance(bar.get(field), bool)]
        if len(set(values)) > 1:
            reasons.append(f"{target} 冻结的未复权/调整响应 {field} 不一致，不能选择性采用证券资格状态")
        if reasons:
            conflicts[condition_id] = list(dict.fromkeys(reasons))
    return conflicts


def _combined(conditions: list[dict], *, technical: bool) -> str:
    states = [row["status"] for row in conditions]
    # An invalid source/version cannot support numerical pass/fail, even if another
    # computed value appears to fail a threshold. Unknown eligibility is separate.
    if technical and any(row["id"] == "data_integrity" and row["status"] != "pass" for row in conditions):
        return "not_computable"
    if "fail" in states:
        return "fail"
    if "unknown" in states or not states:
        return "not_computable" if technical else "pending"
    return "pass"


def evaluate_m21(snapshot: dict) -> dict:
    config = M21Config.model_validate(snapshot["strategy_config"])
    calculation_input = deepcopy(snapshot)
    calculation_input["strategy_config"] = config.calculation_config().model_dump(mode="json")
    report = evaluate_snapshot(calculation_input)
    report.update(workflow_version="m2.1", status_semantics_version=config.status_semantics_version,
                  title="今日方向简报 · M2.1 扩展样本试运行", notice=NOTICE,
                  strategy_config=snapshot["strategy_config"], config_hash=digest(snapshot["strategy_config"]),
                  news_review_status="未完成", announcement_review_status="未完成", research_report_review_status="未完成",
                  sample_selection=snapshot.get("sample_selection", {}),
                  eligibility_evidence_bundle=snapshot.get("eligibility_evidence_bundle", {}))
    for row in report["evaluations"]:
        state = snapshot.get("eligibility_states", {}).get(row["symbol"], {})
        conflicts = _state_conflicts(snapshot, row["symbol"])
        for condition in row["conditions"]:
            if condition["id"] in conflicts:
                condition.update(status="unknown", reason="；".join(conflicts[condition["id"]]))
            if condition["id"] == "not_delisting_period" and state.get("reason"):
                condition["reason"] = state["reason"]
        technical = [deepcopy(item) for item in row["conditions"] if item["id"] in TECHNICAL_IDS]
        eligibility = [deepcopy(item) for item in row["conditions"] if item["id"] in ELIGIBILITY_IDS]
        technical_status = _combined(technical, technical=True)
        eligibility_status = _combined(eligibility, technical=False)
        # Both axes are explicit. A reliable failure still excludes when other
        # checks are unknown; gaps are retained and counted independently.
        if technical_status == "fail" or eligibility_status == "fail":
            status = "excluded"
        elif technical_status == "pass" and eligibility_status == "pass":
            status = "candidate"
        else:
            status = "data_insufficient"
        row.update(technical_screen_status=technical_status, eligibility_status=eligibility_status,
                   technical_conditions=technical, eligibility_conditions=eligibility,
                   delisting_period_status=state.get("status", "true" if state.get("delisting_period") is True else "false" if state.get("delisting_period") is False else "unknown"),
                   eligibility_evidence=state.get("evidence", []),
                   eligibility_evidence_ids=state.get("evidence_ids", []),
                   rejected_eligibility_evidence=state.get("rejected_evidence", []),
                   status=status, rank=None,
                   exclusion_reasons=[item["reason"] for item in row["conditions"] if item["status"] == "fail"],
                   data_issues=list(dict.fromkeys(item["reason"] for item in row["conditions"] if item["status"] == "unknown")),
                   selection_reasons=[item["reason"] for item in row["conditions"] if item["status"] == "pass"] if status == "candidate" else [])
    ranked = sorted((row for row in report["evaluations"] if row["status"] == "candidate"),
                    key=lambda row: (Decimal(row["relative_return"]).copy_negate(), Decimal(row["avg_amount_cny"]).copy_negate(), row["symbol"]))
    for rank, row in enumerate(ranked, 1):
        if rank <= config.max_candidates:
            row["rank"] = rank
        else:
            row["status"] = "qualified_not_selected"
            row["exclusion_reasons"].append(f"条件通过，但排序超出前 {config.max_candidates} 个名额")
    report["candidates"] = ranked[:config.max_candidates]
    report["pending_eligibility"] = sorted((row for row in report["evaluations"] if row["technical_screen_status"] == "pass" and row["eligibility_status"] == "pending"), key=lambda row: row["symbol"])
    report["pending_eligibility_notice"] = "量价达标、资格待核查；不构成已经核验的选股结论，不计正式预候选，不参与正式候选排名。"
    rows = report["evaluations"]
    count = lambda key, value: sum(row[key] == value for row in rows)
    report["counts"] = {
        "stock_count": len(rows), "configured_stock_count": len(rows),
        "market_data_success_count": sum(all(item["status"] != "unknown" for item in row["technical_conditions"]) and row["valid_history_count"] >= config.min_history_trading_days for row in rows),
        "eligibility_verified_count": sum(all(item["status"] != "unknown" for item in row["eligibility_conditions"]) for row in rows),
        "eligibility_conclusion_count": count("eligibility_status", "pass") + count("eligibility_status", "fail"),
        "eligibility_pass_count": count("eligibility_status", "pass"),
        "eligibility_fail_count": count("eligibility_status", "fail"),
        "eligibility_pending_count": count("eligibility_status", "pending"),
        "technical_pass_count": count("technical_screen_status", "pass"),
        "technical_fail_count": count("technical_screen_status", "fail"),
        "technical_not_computable_count": count("technical_screen_status", "not_computable"),
        "candidate_count": len(report["candidates"]), "pending_eligibility_count": len(report["pending_eligibility"]),
        "excluded_count": count("status", "excluded"), "data_insufficient_count": count("status", "data_insufficient"),
        "stocks_with_data_gaps": sum(bool(row["data_issues"]) for row in rows),
        "qualified_not_selected_count": count("status", "qualified_not_selected"),
        "delisting_status_known_count": sum(row["delisting_period_status"] in {"true", "false"} for row in rows),
    }
    report["count_definitions"] = {
        "main_classes": "candidate_count + excluded_count + data_insufficient_count + qualified_not_selected_count = stock_count；互斥主分类。",
        "technical_axis": "technical_pass_count + technical_fail_count + technical_not_computable_count = stock_count。",
        "eligibility_axis": "eligibility_pass_count + eligibility_fail_count + eligibility_pending_count = stock_count。",
        "stocks_with_data_gaps": "存在至少一项 unknown 的股票数，与主分类重叠；已排除的股票仍可能有缺口。",
        "pending_eligibility_count": "量价pass且资格pending的交集；不计candidate_count，不参与候选排名。",
        "eligibility_verified_count": "所有必要资格字段均已判定（无unknown）的股票数量，表示资格资料核验覆盖。",
        "eligibility_conclusion_count": "资格已有明确结论（pass或fail）数量；fail可能因一项明确排除而仍有其他缺口，不表示全部资格字段完整。",
        "market_data_success_count": "冻结窗口历史要求满足，且技术条件均可判断的股票数；不代表真实联网次数或全市场覆盖。",
        "delisting_status_known_count": "具有适用市场和日期证据，退市整理状态可判true/false的股票数。",
    }
    report["gaps"] = list(dict.fromkeys(snapshot.get("calendar_issues", []) + report["benchmark"]["issues"] + [f"{row['symbol']}：{issue}" for row in rows for issue in row["data_issues"]]))
    report["status"] = "non_trading_day" if snapshot.get("target_is_trading") is False else "partial" if report["gaps"] else "market_only"
    report["scope"] = f"扩展样本试运行版：配置 {len(rows)} 只股票、1 个基准指数；不是全市场统计。"
    report["boundaries"] = [NOTICE, report["pending_eligibility_notice"],
        "资格核验仅读取有明确用途和证据范围的状态资料；少量资格公告不等于已完成新闻、公告和研报综合研究。",
        "人工核验记录仅表示按原始出处和有效区间导入的人工结论，不宣称系统自动获取了完整市场资格名单。",
        *[text for text in report["boundaries"] if text != report.get("notice")]]
    report.pop("result_hash", None)
    report["result_hash"] = digest(report)
    return report
