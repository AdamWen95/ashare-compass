"""Shared report sections with escaped Markdown/HTML and deterministic units."""

from html import escape
from importlib.resources import files
import re

from ashare_daily.schemas import ComputedMetric, DailyReport


def format_metric(metric: ComputedMetric) -> str:
    value = metric.value
    if value is None:
        return "未获取（null）"
    if metric.unit == "ratio":
        return f"{value:.2%}"
    if metric.unit == "CNY":
        return f"{value:,.2f} 元"
    if metric.unit == "shares":
        return f"{value:,.0f} 股"
    return f"{value:,.0f} 家"


def sections(report: DailyReport) -> list[tuple[str, list]]:
    """A block is (paragraph|subheading|table, content). Text is never executed."""
    metrics = {metric.metric_id: metric for metric in report.metrics}
    result = []

    def metric_table(ids):
        return ("table", (["指标 ID", "项目", "程序值（人工样本）"], [
            [key, metrics[key].label, format_metric(metrics[key])] for key in ids
        ]))

    def claims(items):
        blocks = []
        labels = {"fact": "合成样本事实", "inference": "DEMO 推论", "opinion": "DEMO 观点"}
        for claim in items:
            blocks.append(("paragraph", f"[{labels[claim.claim_type]}] {claim.text}"))
            if claim.metric_ids:
                blocks.append(metric_table(claim.metric_ids))
            blocks.append(("paragraph", "证据引用：" + ("、".join(claim.evidence_ids) or "无")))
            for prefix, values in (("风险 / 反面证据", claim.risks), ("尚未确认", claim.unknowns)):
                for value in values:
                    blocks.append(("paragraph", f"{prefix}：{value}"))
        return blocks or [("paragraph", "无可展示资料；不代表现实中没有消息或风险。")]

    coverage = report.coverage
    result.append(("数据状态", [
        ("paragraph", report.demo_notice),
        ("table", (["项目", "状态"], [
            ["演示场景日期", report.scenario_date.isoformat()],
            ["真实交易日 / 行情日期", "未核验；trade_date = null"],
            ["资料截点", report.cutoff_at.isoformat()],
            ["实际生成时间", report.actual_generated_at.isoformat()],
            ["时区", "Asia/Shanghai（北京时间）"],
            ["报告状态", f"{report.status}（仅人工样本范围）"],
            ["统计范围", report.scope],
            ["实际覆盖", f"合成样本 {coverage.covered_count} / {coverage.expected_count}；真实市场覆盖未验证"],
            ["样本缺失", "、".join(coverage.missing_security_ids) or "无；仅指合成 fixture"],
            ["样本未知缺失数量", str(coverage.unknown_missing_count)],
            ["筛选范围", "人工指定 DEMO 模板样本；未运行真实筛选"],
        ])),
        ("paragraph", "场景日期可为任意有效日期，包括休市日；程序没有根据星期判断交易日，也没有重建真实历史报告。"),
    ]))
    result.append(("市场复盘", claims(report.market_review)))
    result.append(("重要消息与公告", claims(report.important_news)))
    result.append(("重点方向", claims(report.focus_directions)))
    strategy = report.screening_strategy
    strategy_blocks = [("subheading", strategy.name), ("paragraph", strategy.description),
                       ("paragraph", f"版本：{report.strategy_version}；选择方式：{strategy.selection_mode}")]
    for prefix, values in (("展示规则", strategy.rules), ("排除条件", strategy.exclusions), ("实现边界", strategy.limitations)):
        strategy_blocks.extend(("paragraph", f"{prefix}：{value}") for value in values)
    result.append(("今日选股策略", strategy_blocks))
    candidate_blocks = []
    for candidate in report.candidates:
        candidate_blocks.extend([
            ("subheading", f"{candidate.name} ｜ {candidate.security_id}"),
            ("paragraph", "标签：" + "、".join(candidate.strategy_tags)),
            ("paragraph", f"资料完整度：{candidate.review_status}；仅合成示例，未做真实公告新闻核查。"),
        ])
        candidate_blocks.extend(claims(candidate.claims))
        # Include candidate-level metric references even if absent from claim text.
        already_shown = {key for claim in candidate.claims for key in claim.metric_ids}
        remaining = [key for key in candidate.metric_ids if key not in already_shown]
        if remaining:
            candidate_blocks.append(metric_table(remaining))
        candidate_blocks.append(("paragraph", "业务及行情证据：" + "、".join(candidate.evidence_ids)))
        for prefix, values in (("关键风险", candidate.risks), ("后续研究验证条件", candidate.observation_conditions)):
            candidate_blocks.extend(("paragraph", f"{prefix}：{value}") for value in values)
    result.append(("候选观察池", candidate_blocks or [
        ("paragraph", "本次 DEMO 无候选（0 条）。保留空观察池，不用示例公司补数；这不是对现实市场的筛选结论。"),
    ]))
    result.append(("风险与待验证事项", [("paragraph", value) for value in report.pending_verifications]))
    result.append(("资料覆盖说明", [
        ("paragraph", "新闻、公告、研报均为未获取状态；下列本地资料读取成功不表示任何外部接口正常。模型未调用，提示词未使用。"),
        ("table", (["来源 / 数据集", "访问与状态", "实际获取时间", "说明"], [
            [f"{source.provider} / {source.dataset}", f"{source.access_mode} / {source.status}", source.fetched_at.isoformat(), source.description]
            for source in report.source_health
        ])),
    ]))
    evidence_blocks = []
    for item in report.evidence:
        evidence_blocks.extend([
            ("subheading", f"{item.evidence_id} ｜ {item.title}"),
            ("paragraph", f"出处：{item.provider} / {item.dataset}；类型：{item.content_type}（人工合成，无真实原文）"),
            ("paragraph", f"原文发布时间：不适用（null）；首次观察：{item.first_seen_at.isoformat()}；本次获取：{item.fetched_at.isoformat()}"),
            ("paragraph", f"版本：{item.content_version}；定位：{item.locator}"),
            ("paragraph", f"内容 SHA-256：{item.raw_hash}"),
            ("paragraph", f"冻结内容：{item.frozen_content}"),
        ])
    result.append(("证据索引", evidence_blocks))
    result.append(("程序指标索引", [metric_table(list(metrics)),
        ("paragraph", "JSON 中比例以小数保存；页面转换为百分数。成交额为元，成交量为股；未获取值保持 null，页面显示“未获取”。全部指标仅来自人工合成数据。"),
    ]))
    result.append(("版本与运行信息", [
        ("paragraph", f"快照：{report.snapshot_id}；规则版本：{report.strategy_version}；提示词版本：未使用；模型：未调用。"),
        ("paragraph", "完整输入与证据见同目录 snapshot.json；各文件 SHA-256 见 manifest.json。"),
    ]))
    return result


def _markdown_text(value: object) -> str:
    value = escape(str(value), quote=False).replace("\n", " ").replace("\r", " ")
    value = re.sub(r"([\\`*_{}\[\]()#|~])", r"\\\1", value)
    return re.sub(r"^(\s*)([-+]|\d+[.])(?=\s)", r"\1\\\2", value)


def render_markdown(report: DailyReport) -> str:
    report = DailyReport.model_validate(report.model_dump())
    lines = [f"# {report.title}｜{report.scenario_date}", "", f"> {report.demo_notice}", ""]
    for heading, blocks in sections(report):
        lines.extend([f"## {heading}", ""])
        for kind, value in blocks:
            if kind == "table":
                headers, rows = value
                lines.append("| " + " | ".join(map(_markdown_text, headers)) + " |")
                lines.append("| " + " | ".join("---" for _ in headers) + " |")
                lines.extend("| " + " | ".join(map(_markdown_text, row)) + " |" for row in rows)
            elif kind == "subheading":
                lines.append("### " + _markdown_text(value))
            else:
                lines.append(_markdown_text(value))
            lines.append("")
    return "\n".join(lines)


def render_html(report: DailyReport) -> str:
    report = DailyReport.model_validate(report.model_dump())
    content = []
    for index, (heading, blocks) in enumerate(sections(report)):
        content.append(f'<section id="section-{index}"><h2>{escape(heading)}</h2>')
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
    template = files("ashare_daily").joinpath("reports/templates/demo.html").read_text(encoding="utf-8")
    # Substitute once so marker-like text inside untrusted contents cannot expand.
    values = {"title": escape(report.title), "date": report.scenario_date.isoformat(),
              "notice": escape(report.demo_notice), "body": "\n".join(content)}
    return re.sub(r"\{\{(title|date|notice|body)\}\}", lambda match: values[match[1]], template)
