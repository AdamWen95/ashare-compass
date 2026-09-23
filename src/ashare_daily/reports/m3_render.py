"""M3 fixed report rendering; only validated claims become visible research."""

from __future__ import annotations

import csv
from html import escape
from importlib.resources import files
import json
from pathlib import Path
import re

from ashare_daily.reports.m2_render import _csv_cell, _markdown_text
from ashare_daily.reports.m21_render import m21_sections
from ashare_daily.reports.reader import reader_sections


TYPE_LABELS = {"fact": "资料支持的事实", "inference": "研究推论（待人工语义复核）", "opinion": "来源观点／研究意见",
               "counterevidence": "反面证据", "unknown": "尚未确认", "followup": "后续验证"}


def _text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (list, dict)) else "未取得" if value is None else str(value)


def sections(report: dict) -> list[tuple[str, list]]:
    if report.get("schema_version") != "m3-report-v1":
        raise ValueError("研究模板只接受 M3 存档报告")
    market = report["market"]
    counts = market["counts"]
    status = report["statuses"]
    overview = [("paragraph", report["notice"]), ("table", (["项目", "本次实际记录"], [
        ["行情分析日期", report["trade_date"]], ["实际行情日期", report["actual_market_date"]],
        ["资讯查询区间", f"({report['query_start_at']}, {report['cutoff_at']}]"],
        ["实际生成时间", report["actual_generated_at"]], ["时区", "Asia/Shanghai"],
        ["历史补采", "历史资料补采研究：不冒充截点时已掌握" if report["historical_reconstruction"] else "未采用截点后才首次取得的历史资料"],
        ["行情 / 消息 / 模型 / 生成", f"{status['market']} / {status['messages']} / {status['model']} / {status['generation']}"],
        ["引用结构核验 / 语义复核", f"{status['citation_validation']} / {status['semantic_review']}"],
        ["配置股票 / 行情完整 / 正式预候选 / 资格待查表", f"{counts['stock_count']} / {counts['market_data_success_count']} / {counts['candidate_count']} / {counts['pending_eligibility_count']}"],
        ["本次深入研究对象数 / 上限", f"{len(report['research_objects'])} / {report['research_config']['max_research_objects']}"],
        ["量价策略 / 提示词版本", f"{market['strategy_version']} / {report['prompt_version']}"],
        ["行情快照", market["snapshot_id"]], ["研究输入快照", report["input_snapshot_id"]],
    ])), ("paragraph", "统计仅覆盖冻结样本及登记来源。来源失败或最近若干条返回，不表示当天没有其他新闻、公告或风险。")]
    if report.get("replayed_at"):
        overview.append(("paragraph", "本次离线重放时间：" + report["replayed_at"] + "；原模型结果与原生成时间保留。"))
    claims = report["analysis"]["accepted_claims"]
    research = []
    for kind, label in TYPE_LABELS.items():
        items = [claim for claim in claims if claim["claim_type"] == kind]
        if not items:
            continue
        research.append(("subheading", label))
        for claim in items:
            research.append(("paragraph", f"{claim['claim_id']} ｜ {claim['text']}"))
            if claim.get("symbol"):
                obj = next((obj for obj in report["research_objects"] if obj["symbol"] == claim["symbol"]), {})
                research.append(("paragraph", f"证券 {claim['symbol']}；路径 {obj.get('path', '未确认')}；量价 {obj.get('technical_screen_status')}；资格 {obj.get('eligibility_status')}。事件观察不能视为已经通过趋势筛选。"))
            for citation in claim["citations"]:
                research.append(("paragraph", f"证据 {citation['evidence_id']}；定位 {citation['locator']}；原文短摘录：{citation['quote']}"))
            for metric_id in claim.get("metric_ids", []):
                metric = report["metric_registry"][metric_id]
                research.append(("paragraph", f"程序注入指标 {metric_id}：{_text(metric['value'])} {metric['unit']}；日期 {metric['trade_date']}"))
            if claim.get("risks"):
                research.append(("paragraph", "需核查风险：" + _text(claim["risks"])))
            if claim.get("unknowns"):
                research.append(("paragraph", "未确认：" + _text(claim["unknowns"])))
    if not claims:
        research.append(("paragraph", "未发布模型研究主张：" + report["model_run"]["status"] + "。保留量价基线与真实资料目录，不能理解为无重要风险。"))
    research.append(("paragraph", "引用存在、时间和摘录匹配只说明结构核验通过，不保证推论语义正确。抽查原始资料与结论的支持关系仍然必要。"))
    directions = [("paragraph", "重点方向见开篇的已核验个股研究；资料不足时保留待查事项。"),
                  ("paragraph", "研究思路：沿用既有量价与资格规则，对实际资料中的明确事实查找公司业务依据和反面证据；资格未知继续待核查，事件观察不进入正式量价排名。")]
    if report["research_objects"]:
        directions.append(("table", (["证券", "名称", "研究路径", "量价", "资格", "关联证据"], [
            [obj["symbol"], obj["name"], obj["path"], obj["technical_screen_status"], obj["eligibility_status"], _text(obj["association_evidence_ids"])] for obj in report["research_objects"]])))
    else:
        directions.append(("paragraph", "本次没有形成资料关联充分的个股研究对象；市场消息可以单独研究，不以样例股票填充。"))
    catalog = [("paragraph", "只有正文或足够摘要且许可允许的片段才送入模型。目录条目不是全文；标为背景的旧资料不是本区间新事件。"), ("table", (["证据 ID / 类型", "标题 / 内容层级", "发布时间 / 精度", "首次取得", "来源与定位", "采集方式 / 本次用途"], [
        [item["evidence_id"] + " / " + item["category"], item["title"] + " / " + item["content_type"],
         _text(item.get("published_at")) + " / " + item["publication_precision"], item["first_seen_at"],
         item["original_url"] + " ｜ " + item["raw_locator"], item["acquisition_mode"] + " / " + ("背景" if item.get("is_background") else "区间内资料")]
        for item in report["evidence_catalog"]
    ]))]
    coverage = [("table", (["来源", "类别", "启用 / 实际状态", "访问与内容", "日期/分页范围及限制"], [
        [source["name"], source["category"], f"{source['enabled']} / " + _text(next((r.get("status") for r in report["source_health"] if r.get("source_id") == source["source_id"]), "本次未请求")),
         source["access_method"] + " / " + _text(source["content_access"]), source["supported_date_range"] + "；" + source["pagination_limits"] + "；" + source["usage_limits"]]
        for source in report["source_registry"]
    ])), ("paragraph", "实际返回和失败原因：" + _text(report["source_health"])),
       ("paragraph", "证据筛选/输入长度覆盖：" + _text(report["coverage"])),
       ("paragraph", "模型实际调用、token 和预算：" + _text({k: v for k, v in report["model_run"].items() if k not in {"calls", "responses"}})),
       ("paragraph", "未配置可靠价格时仅记录供应商返回的 token，不推算人民币费用。")]
    gaps = [("paragraph", _text(gap)) for gap in report["gaps"]]
    rejected = [{"claim_id": row.get("claim_id"), "validation_reasons": row.get("validation_reasons", [])}
                for row in report["analysis"]["rejected_claims"]]
    gaps.append(("paragraph", "被拦截的主张编号及原因（原响应仅保留在独立审计存档）：" + _text(rejected)))
    gaps.append(("paragraph", "没有覆盖全市场新闻、全部上市公司公告或研报全文；外部资料中的命令不被执行，模型不能读取本机文件、任意网址或修改数据库。"))
    quant = m21_sections(market)
    return reader_sections(report) + [("一、三个时间与独立模块状态", overview), ("二、重要消息、研究推论与反面证据", research),
            ("三、关注方向与选股研究思路", directions), ("四、资料目录与出处", catalog),
            ("五、来源覆盖与模型状态", coverage), ("六、数据缺口、引用拦截与研究边界", gaps),
            ("七、基准行情（Python 计算）", quant[1][1]), ("八、冻结股票样本与资格状态", quant[2][1]),
            ("九、原始量价规则与排序", quant[3][1]), ("十、原正式预候选", quant[4][1]),
            ("十一、原量价达标、资格待核查表", quant[5][1])]


def render_m3_markdown(report: dict) -> str:
    return render_sections_markdown(report["title"], report["notice"], sections(report))


def render_sections_markdown(report_title: str, notice: str, report_sections: list) -> str:
    """Shared escaped local report format; accepts no model/collector objects."""
    output = [f"# {_markdown_text(report_title)}", "", f"> {_markdown_text(notice)}", ""]
    for title, blocks in report_sections:
        output.extend([f"## {_markdown_text(title)}", ""])
        for kind, value in blocks:
            if kind == "table":
                headers, rows = value
                output.extend(["| " + " | ".join(map(_markdown_text, headers)) + " |", "| " + " | ".join("---" for _ in headers) + " |"])
                output.extend("| " + " | ".join(_markdown_text(str(cell)) for cell in row) + " |" for row in rows)
            else:
                output.append(("### " if kind == "subheading" else "") + _markdown_text(value))
            output.append("")
    return "\n".join(output)


def render_m3_html(report: dict) -> str:
    return render_sections_html(report["title"], report["trade_date"], report["notice"], sections(report))


def render_sections_html(report_title: str, trade_date: str, notice: str, report_sections: list, *, eyebrow=None) -> str:
    """Reuse the existing offline research template and its CSP/escaping."""
    content = []
    for index, (title, blocks) in enumerate(report_sections, 1):
        content.append(f'<section id="section-{index}"><h2>{escape(title)}</h2>')
        for kind, value in blocks:
            if kind == "table":
                headers, rows = value
                content.append('<div class="table-wrap"><table><thead><tr>' + ''.join(f'<th scope="col">{escape(str(v))}</th>' for v in headers) + '</tr></thead><tbody>')
                content.extend('<tr>' + ''.join(f'<td>{escape(str(v))}</td>' for v in row) + '</tr>' for row in rows)
                content.append('</tbody></table></div>')
            else:
                tag = "h3" if kind == "subheading" else "p"
                content.append(f'<{tag}>{escape(value)}</{tag}>')
        content.append('</section>')
    template = files("ashare_daily").joinpath("reports/templates/research.html").read_text(encoding="utf-8")
    if eyebrow is not None:
        template = template.replace("M3 · 证据研究增强 · 仅限登记来源", escape(eyebrow))
    values = {"title": escape(report_title), "date": escape(trade_date), "notice": escape(notice), "body": '\n'.join(content)}
    return re.sub(r'\{\{(title|date|notice|body)\}\}', lambda match: values[match[1]], template)


def write_claim_audit(report: dict, path: Path) -> None:
    rows = report["analysis"].get("claim_evidence_rows", [])
    fields = sorted({key for row in rows for key in row}) or ["claim_id", "evidence_id", "status", "reason"]
    with Path(path).open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_cell(row.get(key)) for key in fields})
