"""M2.1 固定模板：量价状态与资格状态独立展示，旧 M2 模板保持不变。"""

from __future__ import annotations

import csv
from html import escape
from importlib.resources import files
from pathlib import Path
import re

from .m2_render import (
    AUDIT_FIELDS, NUMERIC_FIELDS, _condition_table, _csv_cell, _markdown_text,
    _number, _price, _reason_list, _row_metrics, _status, _text,
    _validate_context, m2_sections,
)


NOTICE = "扩展样本试运行版，仅基于量价规则，尚未完成新闻、公告和研报综合核查；非全市场。"
TECHNICAL_LABELS = {"pass": "量价通过", "fail": "量价不通过", "not_computable": "量价无法计算"}
ELIGIBILITY_LABELS = {"pass": "资格通过", "fail": "资格不通过", "pending": "资格待核实"}
DELISTING_LABELS = {"true": "确认处于退市整理期", "false": "有充分证据确认不处于退市整理期", "unknown": "无法确认"}


def _validate_m21(report: dict) -> None:
    _validate_context(report)
    if report.get("workflow_version") != "m2.1" or report.get("status_semantics_version") != "eligibility-split-v1":
        raise ValueError("M2.1 模板必须绑定工作流与状态语义版本")
    if report.get("news_review_status") != "未完成":
        raise ValueError("M2.1 不提供新闻、公告和研报综合核查")
    rows = report["evaluations"]
    row_map = {row["symbol"]: row for row in rows}
    if len(row_map) != len(rows):
        raise ValueError("股票核验记录不得重复")
    for row in rows:
        if row.get("technical_screen_status") not in TECHNICAL_LABELS or row.get("eligibility_status") not in ELIGIBILITY_LABELS:
            raise ValueError("量价与资格状态必须明确，不能由主分类猜测")
        if row.get("delisting_period_status") not in DELISTING_LABELS:
            raise ValueError("退市整理期必须区分 true、false、unknown")
    candidates = report["candidates"]
    for row in candidates:
        if row["symbol"] not in row_map or row_map[row["symbol"]] != row:
            raise ValueError("候选必须引用完整核验记录")
        if row["technical_screen_status"] != "pass" or row["eligibility_status"] != "pass":
            raise ValueError("正式候选必须量价与资格同时通过")
        if row["status"] != "candidate" or not isinstance(row["rank"], int) or row["rank"] < 1:
            raise ValueError("正式候选必须明确分类与排名")
    if len({row["symbol"] for row in candidates}) != len(candidates):
        raise ValueError("正式候选不得重复")
    pending = report["pending_eligibility"]
    expected_pending = sorted(row["symbol"] for row in rows if row["technical_screen_status"] == "pass" and row["eligibility_status"] == "pending")
    if [row["symbol"] for row in pending] != expected_pending:
        raise ValueError("待核查表必须完整且按证券 ID 排列，不能混入正式排名")
    for row in pending:
        if row != row_map[row["symbol"]] or row["rank"] is not None or row["status"] == "candidate":
            raise ValueError("资格待核查记录不得有正式候选排名")
    counts = report["counts"]
    expected_counts = {
        "stock_count": len(rows), "candidate_count": len(candidates), "pending_eligibility_count": len(pending),
        "technical_pass_count": sum(row["technical_screen_status"] == "pass" for row in rows),
        "technical_fail_count": sum(row["technical_screen_status"] == "fail" for row in rows),
        "technical_not_computable_count": sum(row["technical_screen_status"] == "not_computable" for row in rows),
        "eligibility_pass_count": sum(row["eligibility_status"] == "pass" for row in rows),
        "eligibility_fail_count": sum(row["eligibility_status"] == "fail" for row in rows),
        "eligibility_pending_count": sum(row["eligibility_status"] == "pending" for row in rows),
        "eligibility_verified_count": sum(all(item["status"] != "unknown" for item in row["eligibility_conditions"]) for row in rows),
        "eligibility_conclusion_count": sum(row["eligibility_status"] != "pending" for row in rows),
        "stocks_with_data_gaps": sum(bool(row["data_issues"]) for row in rows),
    }
    for status in ("excluded", "data_insufficient", "qualified_not_selected"):
        expected_counts[f"{status}_count"] = sum(row["status"] == status for row in rows)
    if any(counts.get(key) != expected for key, expected in expected_counts.items()):
        raise ValueError("M2.1 汇总计数必须与逐股记录及独立待核查表一致")


def _split_table(row: dict) -> tuple:
    return ("table", (["核验维度", "明确状态"], [
        ["量价规则", TECHNICAL_LABELS[row["technical_screen_status"]]],
        ["证券资格", ELIGIBILITY_LABELS[row["eligibility_status"]]],
        ["退市整理期", DELISTING_LABELS[row["delisting_period_status"]]],
        ["主分类", _status(row["status"])],
    ]))


def m21_sections(report: dict) -> list[tuple[str, list]]:
    """复用行情与固定策略展示，待核查表单独生成且无排名。"""
    _validate_m21(report)
    base = m2_sections(report)
    counts, config = report["counts"], report["strategy_config"]
    returns, amount = config.get("return_days", 20), config.get("amount_days", 20)
    first = [("paragraph", NOTICE), ("paragraph", report["notice"]), ("table", (["项目", "实际记录"], [
        ["报告状态 / 文件生成状态", f"{_status(report['status'])} / {report.get('generation_status', '见 result.json')}"],
        ["输入来源", "本地真实行情冻结快照" if report["verification_kind"] == "local_real_data" else "OFFLINE TEST · 人工离线测试输入，不是真实行情"],
        ["分析交易日 T", report["trade_date"]], ["实际行情日期", _text(report["actual_market_date"])],
        ["实际生成时间 / 时区", f"{report['actual_generated_at']} / Asia/Shanghai"],
        ["样本范围", report["scope"]],
        ["配置股票样本数 / 本次核验股票数", f"{counts['configured_stock_count']} / {counts['stock_count']}"],
        ["行情成功数", counts["market_data_success_count"]],
        ["资格核验覆盖数（所有必要资格字段均已判定）", counts["eligibility_verified_count"]],
        ["资格已有明确结论数（通过或不通过）", counts["eligibility_conclusion_count"]],
        ["资格通过 / 不通过 / 待核实", f"{counts['eligibility_pass_count']} / {counts['eligibility_fail_count']} / {counts['eligibility_pending_count']}"],
        ["量价通过 / 不通过 / 无法计算", f"{counts['technical_pass_count']} / {counts['technical_fail_count']} / {counts['technical_not_computable_count']}"],
        ["正式预候选数", counts["candidate_count"]],
        ["量价达标、资格待核查数（独立表）", counts["pending_eligibility_count"]],
        ["主分类：正式候选 / 已排除 / 数据不足 / 上限外", f"{counts['candidate_count']} / {counts['excluded_count']} / {counts['data_insufficient_count']} / {counts['qualified_not_selected_count']}"],
        ["存在数据缺口的股票数（重叠计数）", counts["stocks_with_data_gaps"]],
        ["工作流 / 状态语义版本", f"{report['workflow_version']} / {report['status_semantics_version']}"],
        ["输入快照标识", report["snapshot_id"]], ["配置哈希", report["config_hash"]], ["结果哈希", report["result_hash"]],
    ])),
        ("paragraph", "主分类互斥相加等于本次核验股票数；存在数据缺口是重叠计数，可包含已排除股票，不能再与主分类相加。量价轴、资格轴和主分类是对同一批股票的不同描述。"),
        ("paragraph", "资格已有明确不通过结论的股票仍可能有其他未知项；明确结论数不表示每个资格字段均已补齐。资格核验覆盖数只计所有必要资格字段均已判定的股票。待核查数仅计量价通过且资格待核实的交集，不计正式 candidate_count。"),
        ("paragraph", "行情成功数表示冻结窗口历史要求满足、技术条件均可判断，不代表联网次数。详细失败和不足原因见逐股核验及 CSV。"),
        ("paragraph", "生成时间不代表行情日期。后来补取得的历史证据记录实际取得时间，不视为分析日已掌握的资料；证据适用日期与取得时间分别保留。"),
    ]
    if report.get("count_definitions"):
        first.append(("table", (["计数字段", "冻结的计数定义"], [[key, value] for key, value in sorted(report["count_definitions"].items())])))
    overview = [("table", (["证券代码", "名称", "实际行情日期", "收盘价（未复权）", f"{returns} 日相对收益", "量价状态", "资格状态", "主分类"], [
        [row["symbol"], row["name"], _text(row["actual_data_date"]), _price(row["display_close"], row["price_unit"]),
         _number(row["relative_return"], ratio=True), TECHNICAL_LABELS[row["technical_screen_status"]],
         ELIGIBILITY_LABELS[row["eligibility_status"]], _status(row["status"])] for row in report["evaluations"]
    ])), ("paragraph", "只描述配置股票样本；指数只作为基准。不能推断全市场统计、行业轮动、资金流入或政策方向。")]
    strategy = list(base[3][1]) + [
        ("paragraph", "正式预候选必须量价条件与所有必要资格检查同时通过。量价规则或证券资格已有明确失败时可归已排除，但其他缺口继续保留。"),
        ("paragraph", "量价通过但资格待核实仅进入独立待核查表。该表按证券 ID 升序显示，无候选排名，不构成已经核验的选股结论。"),
    ]
    candidates = []
    for row in report["candidates"]:
        candidates.extend([
            ("subheading", f"{row['rank']}. {row['name']} ｜ {row['symbol']}"), _split_table(row),
            ("paragraph", "入选依据：" + _reason_list(row["selection_reasons"])), _row_metrics(row, config), _condition_table(row),
            ("paragraph", "新闻、公告和研报综合核查：未完成。量价与资格通过不表示完成综合研究，也不能证明未来表现。"),
        ])
    if not candidates:
        candidates = [("paragraph", "本期正式量价预候选为 0。保留零候选结果，未放宽参数或用资格未知记录填充。")]
    pending = [("paragraph", "以下仅表示量价达标、资格待核查，不构成已经核验的选股结论。未计入正式 candidate_count，未加入正式候选排名；按证券 ID 升序展示。")]
    if report["pending_eligibility"]:
        pending.append(("table", (["证券代码", "名称", "量价状态", "资格状态", f"{returns} 日相对收益", f"{amount} 日平均成交额（元）", "待核查缺口"], [
            [row["symbol"], row["name"], TECHNICAL_LABELS[row["technical_screen_status"]], ELIGIBILITY_LABELS[row["eligibility_status"]],
             _number(row["relative_return"], ratio=True), _number(row["avg_amount_cny"], digits=2), _reason_list(row["data_issues"])]
            for row in report["pending_eligibility"]
        ])))
    else:
        pending.append(("paragraph", "本期没有量价通过且资格待核实的股票。"))
    audit = []
    for row in report["evaluations"]:
        audit.extend([
            ("subheading", f"{row['symbol']} {row['name']}"), _split_table(row),
            ("paragraph", "明确排除原因：" + _reason_list(row["exclusion_reasons"])),
            ("paragraph", "保留的数据缺口：" + _reason_list(row["data_issues"])),
            _row_metrics(row, config), _condition_table(row),
            ("paragraph", "资格证据（来源、日期、版本及原始定位）：" + _text(row.get("eligibility_evidence", []))),
            ("paragraph", "未采用的资格证据及原因：" + _text(row.get("rejected_eligibility_evidence", []))),
        ])
    if report["non_stock_records"]:
        audit.append(("table", (["非股票代码", "名称", "排除说明"], [[row["symbol"], row["name"], row["reason"]] for row in report["non_stock_records"]])))
    boundaries = [
        ("subheading", "数据缺口与失败原因"),
        *[("paragraph", _text(gap)) for gap in report["gaps"] or ["冻结输入未记录额外缺口；不表示全市场或综合资讯覆盖。"]],
        ("subheading", "资格证据与研究边界"),
        ("paragraph", "退市整理期 true 表示有证据确认处于该状态；false 需要适用于市场和分析日的充分证据；unknown 表示不能确认。上市状态、交易状态与 ST 状态不能互相替代。"),
        ("paragraph", "请求失败、空响应、分页不完整、解析异常或名单覆盖范围不明不能作为空名单排除法证据。证据不适用目标日期、市场或互相冲突时继续待核实。"),
        ("paragraph", "新闻、公告和研报综合核查：未完成。少量用于证券资格核验的官方资料，不等于已接入全面资讯研究；行业分类及业务催化未覆盖。"),
        *[("paragraph", _text(value)) for value in report["boundaries"]],
        ("paragraph", "数值由 Python 确定性计算，使用固定模板、冻结配置与不可变快照；本报告不会被后续补证据或重新抓取覆盖。"),
    ]
    return [("一、数据状态和扩展样本范围", first), base[1], ("三、当前股票样本与双轴状态", overview),
            ("四、本期筛选规则、参数及版本", strategy), ("五、正式量价预候选及入选依据", candidates),
            ("六、量价达标、资格待核查（独立表）", pending), ("七、全部股票逐项核验与资格证据", audit),
            ("八、数据缺口和研究边界", boundaries)]


def render_m21_markdown(report: dict) -> str:
    lines = [f"# {_markdown_text(report['title'])}｜{_markdown_text(report['trade_date'])}", "", f"> {NOTICE}", ""]
    for heading, blocks in m21_sections(report):
        lines.extend([f"## {heading}", ""])
        for kind, value in blocks:
            if kind == "table":
                headers, rows = value
                lines.append("| " + " | ".join(map(_markdown_text, headers)) + " |")
                lines.append("| " + " | ".join("---" for _ in headers) + " |")
                lines.extend("| " + " | ".join(map(_markdown_text, row)) + " |" for row in rows)
            else:
                lines.append(("### " if kind == "subheading" else "") + _markdown_text(value))
            lines.append("")
    return "\n".join(lines)


def render_m21_html(report: dict) -> str:
    content = []
    for index, (heading, blocks) in enumerate(m21_sections(report), start=1):
        content.append(f'<section id="section-{index}"><h2>{escape(heading)}</h2>')
        for kind, value in blocks:
            if kind == "table":
                headers, rows = value
                content.append('<div class="table-wrap"><table><thead><tr>')
                content.extend(f'<th scope="col">{escape(str(cell))}</th>' for cell in headers)
                content.append("</tr></thead><tbody>")
                content.extend("<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>" for row in rows)
                content.append("</tbody></table></div>")
            else:
                tag = "h3" if kind == "subheading" else "p"
                content.append(f"<{tag}>{escape(str(value))}</{tag}>")
        content.append("</section>")
    template = files("ashare_daily").joinpath("reports/templates/market.html").read_text(encoding="utf-8")
    values = {"title": escape(report["title"]), "date": escape(report["trade_date"]), "notice": NOTICE,
              "body": "\n".join(content), "input_kind": "本地真实行情" if report["verification_kind"] == "local_real_data" else "OFFLINE TEST · 人工测试输入"}
    return re.sub(r"\{\{(title|date|notice|body|input_kind)\}\}", lambda match: values[match[1]], template)


def write_m21_audit_csv(report: dict, path: Path | str) -> int:
    """精确数值、全部条件和资格证据同列保留，待核查记录没有排名。"""
    _validate_m21(report)
    condition_ids = sorted({condition["id"] for row in report["evaluations"] for condition in row["conditions"]})
    meta = ["scope_notice", "verification_kind", "workflow_version", "status_semantics_version", "snapshot_id", "strategy_version",
            "metric_windows", "strategy_config", "config_hash", "benchmark_symbol", "benchmark_name", "news_review_status"]
    split_fields = ["technical_screen_status", "eligibility_status", "delisting_period_status", "technical_conditions", "eligibility_conditions", "eligibility_evidence"]
    evidence_fields = ["eligibility_evidence_ids", "rejected_eligibility_evidence"]
    condition_fields = [f"condition_{key}_{part}" for key in condition_ids for part in ("label", "status", "reason")]
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=meta + AUDIT_FIELDS + split_fields + evidence_fields + condition_fields)
        writer.writeheader()
        for row in report["evaluations"]:
            record = {key: row[key] for key in AUDIT_FIELDS + split_fields}
            record.update({key: row.get(key, []) for key in evidence_fields})
            record.update({key: report[key] for key in meta if key in report})
            record.update(scope_notice=NOTICE, benchmark_symbol=report["benchmark"]["symbol"], benchmark_name=report["benchmark"]["name"])
            for condition in row["conditions"]:
                for part in ("label", "status", "reason"):
                    record[f"condition_{condition['id']}_{part}"] = condition[part]
            writer.writerow({key: _csv_cell(value, numeric=key in NUMERIC_FIELDS) for key, value in record.items()})
    return len(report["evaluations"])
