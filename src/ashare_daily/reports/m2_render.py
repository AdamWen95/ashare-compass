"""M2 固定行情模板；只格式化已计算指标，不推断市场事实。"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from html import escape
from importlib.resources import files
import json
from pathlib import Path
import re


NOTICE = "小样本验证版，仅基于量价规则，尚未做新闻、公告和研报核查"
UNKNOWN = "无法判断（数据不足）"
STATUS_LABELS = {
    "candidate": "量价预候选",
    "excluded": "未通过条件",
    "data_insufficient": "数据不足，无法判断",
    "qualified_not_selected": "条件通过，排序未进入上限",
    "market_only": "仅行情研究",
    "partial": "存在数据缺口",
    "non_trading_day": "非交易日，不生成当日行情结论",
}
CONDITION_LABELS = {"pass": "通过", "fail": "未通过", "unknown": "无法判断"}
ADJUSTMENT_LABELS = {
    "unadjusted": "未复权",
    "forward_adjusted": "前复权",
    "backward_adjusted": "后复权",
    "index_native": "指数原生点位（不适用股票复权）",
}


def _validate_context(report: dict) -> None:
    if report.get("mode") != "research":
        raise ValueError("M2 行情模板只接受 research 数据，DEMO 请使用独立模板")
    if report.get("verification_kind") not in {"local_real_data", "offline_test"}:
        raise ValueError("必须明确报告来自本地真实行情或离线测试数据")
    if report.get("timezone") != "Asia/Shanghai":
        raise ValueError("报告时区必须是 Asia/Shanghai")


def _number(value: object, *, digits: int = 4, ratio: bool = False, suffix: str = "") -> str:
    if value is None:
        return UNKNOWN
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("指标必须是有限数值或 null")
    if ratio:
        return f"{number:.4%}"
    return f"{number:,.{digits}f}{suffix}"


def _text(value: object) -> str:
    if value is None:
        return "未获取"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return str(value)


def _adjustment(value: object) -> str:
    return ADJUSTMENT_LABELS.get(value, _text(value))


def _price(value: object, unit: object) -> str:
    return _number(value, suffix={"CNY": " 元", "index_points": " 点"}.get(unit, "（单位未确认）"))


def _status(value: object) -> str:
    return STATUS_LABELS.get(value, _text(value))


def _reason_list(values: list) -> str:
    return "；".join(map(_text, values)) or "无"


def _row_metrics(row: dict, config: dict) -> tuple:
    short, long = config.get("ma_short_days", 20), config.get("ma_long_days", 60)
    returns, amount = config.get("return_days", 20), config.get("amount_days", 20)
    return ("table", (["核验项目", "值 / 口径"], [
        ["分析交易日 / 实际行情日期", f"{row['analysis_date']} / {_text(row['actual_data_date'])}"],
        ["证券类型 / 有效历史数量", f"{_text(row['security_type'])} / {row['valid_history_count']} 个交易日"],
        ["展示收盘价", _price(row['display_close'], row['price_unit'])],
        ["展示价格口径", _adjustment(row['display_adjustment_mode'])],
        ["连续指标价格口径", _adjustment(row['trend_adjustment_mode'])],
        ["趋势序列 C(T)", _price(row['adjusted_close'], row['price_unit'])],
        [f"MA{short} / MA{long}", f"{_price(row['ma_short'], row['price_unit'])} / {_price(row['ma_long'], row['price_unit'])}"],
        [f"{returns} 日收益 / 基准 {returns} 日收益", f"{_number(row['period_return'], ratio=True)} / {_number(row['benchmark_period_return'], ratio=True)}"],
        [f"{returns} 日相对基准收益", _number(row['relative_return'], ratio=True)],
        [f"{amount} 日平均成交额", _number(row['avg_amount_cny'], digits=2, suffix=" 元")],
    ]))


def _condition_table(row: dict) -> tuple:
    return ("table", (["条件 ID", "条件", "判断", "依据 / 缺口"], [
        [condition["id"], condition["label"], CONDITION_LABELS[condition["status"]], condition["reason"]]
        for condition in row["conditions"]
    ]))


def m2_sections(report: dict) -> list[tuple[str, list]]:
    """块结构沿用 M0：paragraph、subheading、table；M0 模板保持独立。"""
    _validate_context(report)
    counts, benchmark = report["counts"], report["benchmark"]
    config = report["strategy_config"]
    short, long = config.get("ma_short_days", 20), config.get("ma_long_days", 60)
    returns, amount = config.get("return_days", 20), config.get("amount_days", 20)
    input_label = "本地 SQLite 真实行情冻结快照" if report["verification_kind"] == "local_real_data" else "OFFLINE TEST · 人工离线测试输入，不是真实行情"
    sections = [("一、数据状态和分析范围", [
        ("paragraph", NOTICE),
        ("paragraph", report["notice"]),
        ("table", (["项目", "实际记录"], [
            ["报告状态", _status(report["status"])],
            ["输入来源", input_label],
            ["分析交易日 T", report["trade_date"]],
            ["实际行情日期（各证券日期详见下表）", _text(report["actual_market_date"])],
            ["实际生成时间", report["actual_generated_at"]],
            ["业务时区", "Asia/Shanghai（北京时间）"],
            ["样本范围", report["scope"]],
            ["股票样本数", counts["stock_count"]],
            ["量价预候选 / 未通过条件 / 数据不足", f"{counts['candidate_count']} / {counts['excluded_count']} / {counts['data_insufficient_count']}"],
            ["条件通过但排序未进入上限", counts["qualified_not_selected_count"]],
            ["输入快照标识", report["snapshot_id"]],
            ["配置哈希", report["config_hash"]],
            ["指标与排序结果哈希", report["result_hash"]],
        ])),
        ("paragraph", "这是按指定分析日生成的版本；生成时间不代表行情日期。事后生成不能视为当时已掌握的实时历史报告。"),
    ])]
    sections.append(("二、基准指数行情概览", [
        ("paragraph", f"基准：{benchmark['name']}（{benchmark['symbol']}），只作为比较基准，不参加股票筛选。"),
        ("table", (["项目", "值 / 口径"], [
            ["实际行情日期", _text(benchmark["actual_data_date"])],
            ["收盘 / 前收盘", f"{_price(benchmark['display_close'], benchmark['price_unit'])} / {_price(benchmark['display_preclose'], benchmark['price_unit'])}"],
            ["日收益", _number(benchmark["daily_return"], ratio=True)],
            [f"{returns} 日收益", _number(benchmark["period_return"], ratio=True)],
            ["展示价格 / 连续指标口径", f"{_adjustment(benchmark['display_adjustment_mode'])} / {_adjustment(benchmark['trend_adjustment_mode'])}"],
            ["基准数据问题", _reason_list(benchmark["issues"])],
        ])),
        ("paragraph", "仅展示上述已有指数；其他指数、全市场涨跌家数、全市场成交额、行业轮动、资金流入和政策受益方向：未覆盖。"),
    ]))
    sections.append(("三、当前股票样本表现概览", [
        ("table", (["证券代码", "名称", "实际行情日期", "收盘价（未复权）", "日收益", f"{returns} 日相对收益", "核验结果"], [
            [row["symbol"], row["name"], _text(row["actual_data_date"]), _price(row["display_close"], row["price_unit"]),
             _number(row["display_daily_return"], ratio=True), _number(row["relative_return"], ratio=True), _status(row["status"])]
            for row in report["evaluations"]
        ])),
        ("paragraph", "本表仅描述已配置的股票样本，不能外推全市场或行业表现。缺失指标保持无法判断，不前向填充或补零。"),
    ]))
    strategy_blocks = [
        ("paragraph", f"策略版本：{report['strategy_version']}。参数来自冻结配置，未为产生候选自动放宽。"),
        ("paragraph", f"量价条件：有效历史 ≥ {config.get('min_history_trading_days', 120)} 个交易日；最近 {amount} 个交易日平均成交额 ≥ {_number(config.get('min_avg_amount_cny', '50000000'), digits=2, suffix=' 元')}；同一复权口径下 C(T) > MA{short}(T) > MA{long}(T)；最近 {returns} 日相对基准收益 > 0。"),
        ("table", (["配置项", "冻结值"], [[key, _text(value)] for key, value in sorted(report["strategy_config"].items())])),
        ("paragraph", f"MA{short}、MA{long} 分别使用截至 T 的对应交易日窗口收盘均值；{returns} 日收益 = C(T) / C(T-{returns}) - 1，需要 {returns + 1} 个价格点。相对收益 = 股票 {returns} 日收益 − 基准 {returns} 日收益，二者必须使用相同起止交易日。"),
        ("paragraph", f"按相对基准收益降序、{amount} 日平均成交额降序、证券 ID 升序排列，通过条件后最多保留 {config.get('max_candidates', 20)} 个量价预候选。退市整理、ST、停牌或身份状态不明须按条件核验。"),
        ("paragraph", "趋势与跨日收益使用冻结的同来源同口径序列，展示收盘价使用未复权价格；成交额统一为元。精确数值与逐项判断见同目录 screening_audit.csv 和 JSON。"),
    ]
    sections.append(("四、本期筛选规则、参数及版本", strategy_blocks))
    candidate_blocks = []
    for row in report["candidates"]:
        candidate_blocks.extend([
            ("subheading", f"{row['rank']}. {row['name']} ｜ {row['symbol']}"),
            ("paragraph", "状态：量价预筛选通过；新闻、公告、研报核查：未覆盖。"),
            ("paragraph", "入选依据：" + _reason_list(row["selection_reasons"])),
            _row_metrics(row, config), _condition_table(row),
            ("paragraph", "待核查：业务关联、信息催化、公司公告和反面证据均未覆盖；量价条件不能证明未来表现。"),
        ])
    sections.append(("五、量价预候选及入选依据", candidate_blocks or [
        ("paragraph", "本期量价预候选为 0。保持零候选结果；逐股条件与缺口见下一节。"),
        ("paragraph", "零候选可能来自未通过条件或数据不足，两类结果分开记录，不表示市场中不存在其他研究机会。"),
    ]))
    excluded_blocks = []
    for row in report["evaluations"]:
        if row["status"] == "candidate":
            continue
        excluded_blocks.extend([
            ("subheading", f"{row['name']} ｜ {row['symbol']} · {_status(row['status'])}"),
            ("paragraph", "最终排除原因：" + _reason_list(row["exclusion_reasons"])),
            ("paragraph", "数据问题：" + _reason_list(row["data_issues"])),
            _row_metrics(row, config), _condition_table(row),
        ])
    if report["non_stock_records"]:
        excluded_blocks.extend([
            ("subheading", "非股票筛选对象"),
            ("table", (["代码", "名称", "排除说明"], [[row["symbol"], row["name"], row["reason"]] for row in report["non_stock_records"]])),
        ])
    sections.append(("六、未通过条件与无法判断的逐股记录", excluded_blocks or [("paragraph", "本期股票样本均已进入量价预候选，无其他排除记录。")]))
    sections.append(("七、数据缺口和研究边界", [
        ("subheading", "数据缺口"),
        *[("paragraph", _text(value)) for value in report["gaps"] or ["输入范围内未记录额外数据缺口；这不表示全市场覆盖。"]],
        ("subheading", "研究边界"),
        *[("paragraph", _text(value)) for value in report["boundaries"]],
        ("paragraph", "重要消息、公司公告、研报、行业分类及业务催化：未覆盖。没有资料不能解释为没有风险。"),
        ("paragraph", "所有数值由 Python 确定性计算，固定模板生成；没有模型调用。报告关联不可变输入快照与策略配置；后续抓取不会覆盖此版本的原始依据。"),
    ]))
    return sections


def _markdown_text(value: object) -> str:
    value = escape(str(value), quote=False).replace("\n", " ").replace("\r", " ")
    value = re.sub(r"([\\`*_{}\[\]()#|~])", r"\\\1", value)
    return re.sub(r"^(\s*)([-+]|\d+[.])(?=\s)", r"\1\\\2", value)


def render_m2_markdown(report: dict) -> str:
    sections = m2_sections(report)
    lines = [f"# {_markdown_text(report['title'])}｜{_markdown_text(report['trade_date'])}", "", f"> {NOTICE}", ""]
    for heading, blocks in sections:
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


def render_m2_html(report: dict) -> str:
    content = []
    for index, (heading, blocks) in enumerate(m2_sections(report)):
        content.append(f'<section id="section-{index + 1}"><h2>{escape(heading)}</h2>')
        for kind, value in blocks:
            if kind == "table":
                headers, rows = value
                content.append('<div class="table-wrap"><table><thead><tr>')
                content.extend(f'<th scope="col">{escape(str(cell))}</th>' for cell in headers)
                content.append("</tr></thead><tbody>")
                for row in rows:
                    content.append("<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>")
                content.append("</tbody></table></div>")
            else:
                tag = "h3" if kind == "subheading" else "p"
                content.append(f"<{tag}>{escape(str(value))}</{tag}>")
        content.append("</section>")
    template = files("ashare_daily").joinpath("reports/templates/market.html").read_text(encoding="utf-8")
    values = {"title": escape(report["title"]), "date": escape(report["trade_date"]), "notice": NOTICE,
              "body": "\n".join(content), "input_kind": "本地真实行情" if report["verification_kind"] == "local_real_data" else "OFFLINE TEST · 人工测试输入"}
    # 单次替换，来源文本中的模板标记不会再次展开。
    return re.sub(r"\{\{(title|date|notice|body|input_kind)\}\}", lambda match: values[match[1]], template)


AUDIT_FIELDS = [
    "symbol", "name", "analysis_date", "actual_data_date", "security_type", "valid_history_count",
    "price_unit", "display_adjustment_mode", "trend_adjustment_mode", "display_close", "display_daily_return",
    "adjusted_close", "ma_short", "ma_long", "period_return", "benchmark_period_return", "relative_return",
    "avg_amount_cny", "status", "rank", "exclusion_reasons", "data_issues", "selection_reasons",
]
NUMERIC_FIELDS = {
    "valid_history_count", "display_close", "display_daily_return", "adjusted_close", "ma_short", "ma_long",
    "period_return", "benchmark_period_return", "relative_return", "avg_amount_cny", "rank",
}


def _csv_cell(value: object, *, numeric: bool = False) -> str:
    if value is None:
        return ""
    text = _text(value)
    if numeric:
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("CSV 数值列必须为精确数字或 null") from exc
        if not parsed.is_finite():
            raise ValueError("CSV 数值列必须为有限数值")
        # 只放行数字语法，拒绝可能被表格软件执行的数值前缀文本。
        if not re.fullmatch(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
            raise ValueError("CSV 数值文本格式不安全")
        return text
    if text and (text[0].isspace() or text.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + text
    return text


def write_audit_csv(report: dict, path: Path | str) -> int:
    """所有股票一行一个核验记录；比例保留小数、金额保留元、null 为空。"""
    _validate_context(report)
    condition_ids = sorted({condition["id"] for row in report["evaluations"] for condition in row["conditions"]})
    meta_fields = ["scope_notice", "verification_kind", "snapshot_id", "strategy_version", "metric_windows", "strategy_config", "config_hash", "benchmark_symbol", "benchmark_name"]
    condition_fields = [f"condition_{key}_{part}" for key in condition_ids for part in ("label", "status", "reason")]
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=meta_fields + AUDIT_FIELDS + condition_fields)
        writer.writeheader()
        for row in report["evaluations"]:
            record = {key: row[key] for key in AUDIT_FIELDS}
            record.update(scope_notice=NOTICE, verification_kind=report["verification_kind"], snapshot_id=report["snapshot_id"],
                          strategy_version=report["strategy_version"], metric_windows=report["metric_windows"],
                          strategy_config=report["strategy_config"], config_hash=report["config_hash"],
                          benchmark_symbol=report["benchmark"]["symbol"], benchmark_name=report["benchmark"]["name"])
            for condition in row["conditions"]:
                for part in ("label", "status", "reason"):
                    record[f"condition_{condition['id']}_{part}"] = condition[part]
            writer.writerow({key: _csv_cell(value, numeric=key in NUMERIC_FIELDS) for key, value in record.items()})
    return len(report["evaluations"])
