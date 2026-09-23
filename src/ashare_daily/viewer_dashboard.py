"""Compact, read-only presentation of an already verified observation archive.

No collection, scoring, or archive writes happen here. Every HTML value is
escaped; archived HTML is never inserted into the application.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo


STYLE = """
<style>
:root { --brief-ink:#3d2b24; --brief-muted:#79675c; --brief-accent:#bd432b;
  --brief-line:#ebdfd5; --brief-font:"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif; }
.stApp { background:#faf6f1; color:var(--brief-ink); font-family:var(--brief-font); }
.stApp h1,.stApp h2,.stApp h3,.stApp p,.stApp input,.stApp button,.stApp label,
[data-testid="stMetricValue"],[data-baseweb="select"],.stApp [data-testid="stMarkdownContainer"] { font-family:var(--brief-font); }
[data-testid="stHeader"] { background:transparent; }
[data-testid="stMainBlockContainer"] { max-width:1480px; padding:2.5rem 3rem 4rem; }
[data-testid="stSidebar"] { background:#fffdf9; border-right:1px solid var(--brief-line); }
[data-testid="stSidebarUserContent"] { padding-top:1.3rem; }
[data-testid="stCaptionContainer"] { color:var(--brief-muted); font-size:12px; line-height:1.8; }
h1 { font-size:1.9rem!important; letter-spacing:-.04em; font-weight:700!important; }
h2 { font-size:1.25rem!important; letter-spacing:-.02em; } h3 { font-size:1.08rem!important; }
[data-testid="stMain"] [data-testid="stHeading"] { margin-top:8px; }
[data-testid="stMetric"] { background:#fff; border:1px solid var(--brief-line); border-radius:14px;
  padding:18px 22px; box-shadow:0 3px 12px #763c2003; }
[data-testid="stMetricLabel"] { color:var(--brief-muted); font-size:12px; }
[data-testid="stMetricValue"] { font-size:2.2rem; font-weight:650; letter-spacing:-.04em;
  color:var(--brief-ink); font-variant-numeric:tabular-nums; }
[data-testid="stMain"] [data-testid="stColumn"]:first-child [data-testid="stMetric"] {
  background:#fff0e5; border-color:#efc9b3; }
[data-testid="stMain"] [data-testid="stColumn"]:first-child [data-testid="stMetricValue"] { color:var(--brief-accent); }
[data-baseweb="select"]>div,[data-testid="stTextInput"] [data-baseweb="input"] {
  background:#fff; border-color:var(--brief-line); border-radius:9px; }
[data-testid="stButton"] button,[data-testid="stDownloadButton"] button {
  border-color:#e7d7ca; border-radius:9px; font-size:13px; }
[data-testid="stButton"] button:hover,[data-testid="stDownloadButton"] button:hover {
  border-color:var(--brief-accent); color:var(--brief-accent); background:#fff0e5; }
[data-testid="stExpander"] { border-color:var(--brief-line); border-radius:10px; background:#ffffff80; }
[data-testid="stDataFrame"] { border-radius:12px; overflow:hidden; }
[data-testid="stRadio"] [role="radiogroup"] { gap:8px; flex-wrap:wrap; }
.st-key-observation_view { border-bottom:1px solid var(--brief-line); padding:4px 0 13px; margin:2px 0 3px; }
.st-key-observation_view [role="radiogroup"] { gap:6px; }
.st-key-observation_view label { border-radius:8px; padding:10px 17px; color:#79675c; margin:0; }
.st-key-observation_view [data-testid="stRadioOption"]>div>div>div:first-child { display:none; }
.st-key-observation_view label:has(input:checked) { background:#fff; color:#ac3525; box-shadow:0 1px 5px #763c2012; }
.st-key-observation_view label:has(input:checked) p { font-weight:650; }
.st-key-observation_view label:has(input:focus-visible) { outline:2px solid var(--brief-accent); outline-offset:3px; }
.st-key-observation_view label:hover { background:#fae9dd; }
.brief-brand { display:flex; align-items:center; gap:12px; margin:0 0 28px; }
.brief-brand-mark { display:grid; place-items:center; width:42px; height:42px; flex-shrink:0;
  border-radius:13px; background:#b9422a; color:white; font-size:22px; font-weight:650; }
.brief-brand strong { display:block; font-size:16px; letter-spacing:.06em; }
.brief-brand small { display:block; color:#856957; font-size:10px; letter-spacing:.16em; margin-top:4px; }
.brief-masthead { display:flex; justify-content:space-between; align-items:center; gap:20px; margin:0 0 20px; }
.brief-kicker { color:#9b583d; font-size:10px; letter-spacing:.18em; font-weight:650; margin-bottom:9px; }
.brief-masthead h1 { margin:0; padding:0; line-height:1.4; color:#4b2d24; }
.brief-masthead p { color:#79675c; font-size:12px; margin:8px 0 0; }
.brief-readonly { color:#985234; font-size:11px; border:1px solid #eed5c3; border-radius:20px;
  padding:7px 12px; white-space:nowrap; background:#ffffff80; }
.brief-readonly::before { content:""; display:inline-block; width:6px; height:6px; border-radius:50%;
  background:#c66a38; margin-right:7px; }
.brief-hero { display:flex; align-items:center; justify-content:space-between; gap:26px;
  background:linear-gradient(115deg,#9e2c24 0%,#bb4726 65%,#b35222 100%); color:#fff;
  border:1px solid #b54428; border-radius:18px;
  padding:28px 32px; margin:0 0 2px; box-shadow:0 8px 20px #a6451812; }
.brief-hero-copy { min-width:0; }
.brief-eyebrow { font-size:10px; font-weight:600; letter-spacing:.15em; color:#fff0e1; margin-bottom:13px; }
.brief-hero h2 { color:#fff; padding:0; margin:0 0 14px; font-size:1.55rem!important; line-height:1.55; }
.brief-hero p { color:#fff5ec; margin:7px 0 0; line-height:1.8; font-size:12px; }
.brief-date { flex-shrink:0; min-width:115px; padding-left:26px; border-left:1px solid #ffffff26; text-align:center; }
.brief-date small { display:block; color:#fff5ec; font-size:11px; letter-spacing:.05em; }
.brief-date strong { display:block; font-size:30px; font-weight:500; letter-spacing:-.04em; margin:7px 0; }
.brief-date span { color:#fff5ec; font-size:10px; letter-spacing:.12em; }
.brief-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; margin:6px 0 14px; }
.brief-card { min-width:0; display:flex; flex-direction:column; background:#fff; border:1px solid var(--brief-line);
  border-radius:15px; padding:22px; box-shadow:0 3px 12px #763c2003; }
.brief-card-top { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; }
.brief-rank { color:#79675c; font-size:11px; letter-spacing:.04em; font-variant-numeric:tabular-nums; }
.brief-badge { display:inline-block; font-size:10px; border-radius:5px; padding:4px 7px; background:#fff0e5; color:#a84722; }
.brief-badge.pending { background:#fcf2df; color:#936a27; }
.brief-name { margin-top:19px; font-weight:650; font-size:22px; line-height:1.4; letter-spacing:.02em; overflow-wrap:anywhere; }
.brief-symbol { color:#79675c; font-size:12px; letter-spacing:.06em; margin-top:4px; }
.brief-numbers { display:flex; gap:14px; margin:20px 0 15px; padding:15px 0;
  border-top:1px solid #f1e7df; border-bottom:1px solid #f1e7df; }
.brief-numbers>div { flex:1; min-width:0; }
.brief-numbers strong { font-size:19px; font-weight:650; display:block; letter-spacing:-.04em;
  font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
.brief-numbers small { display:block; color:#79675c; font-size:11px; margin-top:7px; line-height:1.6; }
.brief-up { color:#c23b31; } .brief-down { color:#19715d; } .brief-neutral { color:#79675c; }
.stApp .brief-copy { color:#746359; font-size:13px; line-height:1.9; margin:6px 0; overflow-wrap:anywhere; }
.brief-copy b { color:#664939; font-size:11px; font-weight:650; }
.stApp .brief-score { font-size:12px; color:#855139; padding:10px 0 13px; margin-top:auto; }
.stApp .brief-risk { margin:0; padding:12px 13px; border-radius:8px; color:#77613b; background:#fff5e5; font-size:12px; }
.brief-risk b { color:#876c3d; }
.brief-sector { display:flex; flex-wrap:wrap; gap:10px 24px; align-items:center; background:#fff;
  border:1px solid var(--brief-line); border-left:3px solid #d9996c; border-radius:10px; padding:18px 22px; margin:5px 0; }
.brief-sector strong { font-size:15px; flex:1; min-width:180px; }
.brief-sector span { font-size:12px; color:#79675c; }
.brief-empty { background:#fff; border:1px dashed #dabda8; border-radius:14px; padding:30px; color:#79675c; line-height:1.9; }
@media(min-width:1600px) { .brief-grid { gap:22px; } .brief-card { padding:26px; } }
@media(max-width:1200px) { .brief-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
  [data-testid="stMainBlockContainer"] { padding-right:2rem; padding-left:2rem; } .brief-date { display:none; } }
@media(max-width:640px) { .brief-grid { grid-template-columns:1fr; gap:14px; } .brief-hero { padding:22px; border-radius:14px; }
  .brief-hero h2 { font-size:1.3rem!important; } .brief-masthead { margin-bottom:14px; }
  .brief-masthead h1 { font-size:1.6rem!important; } .brief-readonly { display:none; }
  .brief-masthead p { font-size:11px; } .brief-kicker { font-size:9px; }
  .brief-sector { padding:16px; gap:8px; } .brief-sector span { display:block; width:100%; }
  [data-testid="stMainBlockContainer"] { padding:1.2rem 1rem 3rem; }
  [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) { flex-wrap:wrap; gap:10px; }
  [data-testid="stMain"] [data-testid="stColumn"]:has([data-testid="stMetric"]) { min-width:calc(50% - 10px)!important; flex:1 1 calc(50% - 10px)!important; }
  [data-testid="stMetric"] { padding:14px 15px; } [data-testid="stMetricValue"] { font-size:1.8rem; }
  .st-key-observation_view label { padding:9px 12px; } .st-key-observation_view label p { font-size:12px; }
}
</style>
"""


def apply_theme(st):
    st.markdown(STYLE, unsafe_allow_html=True)


MASTHEAD = ('<header class="brief-masthead"><div><div class="brief-kicker">A-SHARE / DAILY RESEARCH</div>'
        '<h1>今日方向简报</h1><p>盘后研究 · 候选观察 · 事实依据与风险 ｜ 北京时间</p></div>'
        '<span class="brief-readonly">只读研究</span></header>')
SIDEBAR_BRAND = ('<div class="brief-brand"><span class="brief-brand-mark" aria-hidden="true">研</span>'
        '<div><strong>方向研究室</strong><small>DAILY RESEARCH</small></div></div>')


def render_masthead(st):
    st.markdown(MASTHEAD, unsafe_allow_html=True)
    st.sidebar.markdown(SIDEBAR_BRAND, unsafe_allow_html=True)


def _safe(value):
    from .viewer import public_value
    return escape(str(public_value(value)), quote=True)


def _number(value, *, scale=1, suffix="", signed=False):
    if value is None:
        return "未取得"
    try:
        number = Decimal(str(value)) * Decimal(str(scale))
        if not number.is_finite():
            return "未取得"
        return format(number, "+,.2f" if signed else ",.2f") + suffix
    except (InvalidOperation, ValueError):
        return "未取得"


def focus_rows(report):
    """Retain formal ranking; pending shadow display matches frozen report rules."""
    if report["observations"]:
        return report["observations"][:5], False
    records = report.get("reference_review", {}).get("records", [])
    pending = {row["security_id"]: row for row in report["pending"]}
    ranked = sorted((r for r in records if r.get("baseline_technical_status") == "pass"
        and r.get("eligibility_status") == "pending" and r.get("status") == "available"
        and r["security_id"] in pending), key=lambda r: (-r["score"], r["symbol"], r["security_id"]))
    # Older archives have no auxiliary scoring. Keep their saved order explicit.
    return ([pending[r["security_id"]] for r in ranked[:5]] if records else report["pending"][:5]), True


def _gap_labels(row):
    labels = {"delisting_period": "退市整理期状态", "st": "ST状态", "suspended": "停牌状态",
              "listed": "上市状态", "identity": "证券身份"}
    values = [labels.get(g.get("field"), g.get("field", "资格证据"))
              for g in row.get("eligibility_gaps", []) if isinstance(g, dict)]
    return "、".join(dict.fromkeys(values)) or "基本资格证据"


def cards_html(report, rows, pending):
    references = {r["security_id"]: r for r in report.get("reference_review", {}).get("records", [])}
    cards = []
    for rank, row in enumerate(rows, 1):
        metrics = row.get("metrics", {})
        reference = references.get(row["security_id"], {})
        reasons = [c["label"] for c in row.get("technical_conditions", [])
                   if c.get("status") == "pass" and c.get("id") in {"trend", "relative_strength", "liquidity"}]
        reason = "；".join(reasons) or "本期冻结量价条件通过；逐项依据见个股详情。"
        risk = ("待补：" + _gap_labels(row) + "。" if pending else "") + row.get("company_risk_notice", "公司材料尚未完成核查")
        relative = _number(metrics.get("relative_return_20"), scale=100, suffix=" pp", signed=True)
        tone = "brief-neutral" if relative == "未取得" else "brief-down" if relative.startswith("-") else "brief-up"
        score = (f"辅助分 {_safe(reference['score'])} · 试运行，不代表上涨概率"
                 if reference.get("status") == "available" else "辅助评分未取得")
        cards.append(f'<article class="brief-card"><div class="brief-card-top">'
            f'<span class="brief-rank">{rank:02d} / 重点观察</span>'
            f'<span class="brief-badge {"pending" if pending else ""}">{"资格待查" if pending else "本期条件通过" if "eligibility_policy" in report else "原规则通过"}</span></div>'
            f'<div class="brief-name">{_safe(row["name"])}</div><div class="brief-symbol">{_safe(row["symbol"])}</div>'
            f'<div class="brief-numbers"><div><strong class="{tone}">{relative}</strong><small>20日相对上证 · 百分点</small></div>'
            f'<div><strong>{_number(metrics.get("avg_amount_20_cny"), scale="0.0001", suffix=" 万")}</strong><small>20日平均成交额 · 元</small></div></div>'
            f'<p class="brief-copy"><b>观察依据</b><br>{_safe(reason)}</p>'
            f'<p class="brief-copy brief-score">{score}</p><p class="brief-copy brief-risk"><b>风险与待查</b><br>{_safe(risk)}</p></article>')
    return '<div class="brief-grid">' + "".join(cards) + '</div>'


def _summary(st, report):
    from .viewer import _label
    from .reports.observation import eligibility_scope_notice
    counts = report["counts"]
    sectors = [s for s in report.get("sector_comparison", []) if s.get("selected")]
    direction = "、".join(s["name"] for s in sectors) or "暂无入选行业方向"
    conclusion = (f"{counts['observation_count']}只研究候选，进入重点核查"
                  if counts["observation_count"] else "正式候选为0，先看待核查观察" if counts["pending_count"]
                  else "正式候选为0，查看未通过原因与数据缺口")
    if not counts["stock_count"]:
        conclusion = "本期暂无满足规则的研究候选"
    html = (f'<section class="brief-hero"><div class="brief-hero-copy"><div class="brief-eyebrow">本期研究摘要 / DAILY BRIEF</div>'
        f'<h2>{_safe(conclusion)}</h2><p>关注方向：{_safe(direction)}</p>'
        f'<p>行业内覆盖 {_safe(counts["stock_count"])} 只 · 量价通过 {_safe(counts["technical_pass_count"])} 只 · '
        f'其中资格待查 {_safe(counts["pending_count"])} 只。先核对筛选依据，再阅读风险与证据。</p></div>'
        f'<div class="brief-date"><small>行情日期</small><strong>{_safe(report["trade_date"][5:])}</strong>'
        f'<span>{_safe(report["trade_date"][:4])} · 盘后研究</span></div></section>')
    st.markdown(html, unsafe_allow_html=True)
    stamp = datetime.fromisoformat(report["actual_generated_at"]).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    st.caption(_label(f"行情日期 {report['trade_date']} · 生成于 {stamp}（北京时间）"
        + (" · 历史资料补采研究，保留实际取得时间" if report.get("historical_reconstruction") else " · 已冻结的盘后研究")))
    policy_notice = eligibility_scope_notice(report)
    if policy_notice:
        st.caption(policy_notice)
    for column, (key, label) in zip(st.columns(4), (("observation_count", "正式研究候选"),
            ("pending_count", "量价达标 · 资格待查"), ("stock_count", "行业内研究范围"),
            ("technical_unknown_count", "技术输入不足"))):
        column.metric(label, counts[key])


def _directions(st, report):
    sectors = sorted((s for s in report.get("sector_comparison", []) if s.get("selected")),
                     key=lambda s: s.get("selected_rank") or 999)
    st.subheader("今日关注方向")
    if sectors:
        for sector in sectors:
            value = _number(sector.get("ranking_change_pct"), suffix="%", signed=True)
            st.markdown(f'<div class="brief-sector"><strong>{_safe(sector["name"])}</strong>'
                f'<span>成分日涨幅均值 {value}</span><span>范围内成分 {_safe(sector.get("scope_member_count", "未记录"))} 只</span></div>',
                unsafe_allow_html=True)
        st.caption("方向来自本期行业筛选；日涨幅为成分均值，不是行业指数涨幅，也不代表公司已被证实受益。")
    else:
        st.info("本期没有已记录的入选行业方向；不根据个股名称推断所属行业。")


def _overview(st, report):
    from .viewer import _label
    st.subheader("重点研究候选")
    rows, pending = focus_rows(report)
    if pending:
        st.caption("本期没有同时满足量价及基本资格条件的研究候选，保留空名单。")
        if rows:
            st.subheader("待核查观察 · 非正式候选")
            st.caption("以下资格待查，不是正式推荐。" + ("沿用报告中的辅助评分顺序，最多5只。" if report.get("reference_review") else "按已保存待查名单顺序，最多5只。"))
    else:
        st.caption(_label(report["ranking_basis"]) + "；本页重点展示前5只。")
    if rows:
        st.markdown(cards_html(report, rows, pending), unsafe_allow_html=True)
        st.caption("后续观察条件：继续核验趋势、相对表现、成交活跃度及基本资格；公告、经营变化与相对强势回落均可能削弱依据。个股详情可核对完整数据。")
    else:
        st.markdown('<div class="brief-empty">本期暂无可展示的重点观察股票。可在「筛选明细」检查未通过条件和数据缺口。</div>', unsafe_allow_html=True)
    if report.get("reference_review"):
        st.caption("参考策略技术辅助评分（试运行） · 仅作辅助，不改变正式候选资格或排序，尚未证明优于原策略。")
    _directions(st, report)
    st.caption(_label(report["notice"]))


def _blocks(st, blocks):
    from .viewer import _prose, _table, _label
    for kind, value in blocks:
        if kind == "paragraph":
            _prose(st, value)
        elif kind == "subheading":
            st.caption(_label(value))
        elif kind == "table":
            headers, rows = value
            _table(st, [{str(h): "未取得" if cell is None else str(cell) for h, cell in zip(headers, row)} for row in rows])


def _detail(st, report):
    from .viewer import _label, _table, _prose, status_label
    rows, pending = focus_rows(report)
    all_rows = rows + [row for row in report["evaluations"] if row["security_id"] not in {r["security_id"] for r in rows}]
    st.subheader("个股详情与研究依据")
    if not all_rows:
        st.info("本期没有个股核验记录。")
        return
    selected = st.selectbox("选择股票", range(len(all_rows)),
        format_func=lambda i: _label(all_rows[i]["name"] + " · " + all_rows[i]["symbol"]), key="observation_stock_detail")
    row = all_rows[selected]
    st.subheader(_label(row["name"] + " · " + row["symbol"]))
    st.caption("量价：" + status_label(row["technical_status"]) + " · 基本资格：" + status_label(row["eligibility_status"]))
    st.warning(_label(row.get("company_risk_notice", "公司材料未完成核查")))
    _table(st, [{"指标": label, "冻结值": _number(row.get("metrics", {}).get(key), scale=scale, suffix=suffix)}
        for key, label, scale, suffix in (("adjusted_close", "前复权收盘（非实时价格）", 1, ""),
        ("ma20", "MA20", 1, ""), ("ma60", "MA60", 1, ""),
        ("relative_return_20", "20日相对上证（百分点）", 100, " pp"),
        ("avg_amount_20_cny", "20日平均成交额（元）", 1, ""))])
    for key, title in (("technical_conditions", "量价筛选条件"), ("eligibility_conditions", "基本资格核验")):
        st.subheader(title)
        _table(st, [{"条件": c.get("label"),
                    "结论": "不参与资格筛选" if c.get("status") == "not_required" else status_label(c.get("status")),
                    "依据": (str(c.get("policy_reason", "")) + " 已有证据状态：" + status_label(c.get("evidence_status")) + "；" + str(c.get("reason", "")))
                        if c.get("status") == "not_required" else c.get("reason")}
                    for c in row.get(key, [])])
    if row.get("eligibility_gaps"):
        st.warning("待补证据：" + _label(_gap_labels(row)))
    reference = next((r for r in report.get("reference_review", {}).get("records", []) if r["security_id"] == row["security_id"]), None)
    if reference:
        st.subheader("参考策略技术辅助评分（试运行）")
        if reference["status"] == "available":
            st.caption(f"辅助分 {reference['score']} · 并非上涨概率，不改变原排名")
            st.caption(_label(f"计算窗口 {reference['window_start']} 至 {reference['window_end']}"))
            _table(st, [{"技术项目": c["label"], "加减分": f"{c['points']:+d}", "依据": c["reason"]} for c in reference["components"]])
        else:
            st.info("本期辅助评分未取得。")
        for issue in reference.get("issues", []):
            _prose(st, issue)
    if "financial_review" in report:
        from .reports.observation import _financial_blocks
        st.subheader("财务事实与反面证据")
        record = next((r for r in report["financial_review"]["records"] if r["security_id"] == row["security_id"]), None)
        _blocks(st, _financial_blocks(record))
    st.info("后续观察条件：下一期继续核验趋势、相对表现、成交活跃度和基本资格。量价可能回落，尚未核查的公告或经营变化可能削弱当前依据。")


def _audit(st, report):
    from .viewer import _table, status_label
    st.subheader("筛选明细与数据缺口")
    query = st.text_input("搜索股票名称或代码", key="observation_search").strip().lower()
    choice = st.radio("筛选状态", ["全部", "正式候选", "资格待查", "技术输入不足", "未通过"], horizontal=True, key="observation_filter")
    formal = {row["security_id"] for row in report["observations"]}
    rows = [row for row in report["evaluations"] if not query or query in (row["symbol"] + row["name"]).lower()]
    filters = {"正式候选": lambda r: r["security_id"] in formal,
        "资格待查": lambda r: r["technical_status"] == "pass" and r["eligibility_status"] == "pending",
        "技术输入不足": lambda r: r["technical_status"] == "unknown",
        "未通过": lambda r: r["technical_status"] == "fail" or r["eligibility_status"] == "fail"}
    if choice in filters:
        rows = [r for r in rows if filters[choice](r)]
    pages = max(1, (len(rows) + 49) // 50)
    page = st.selectbox("明细页", range(pages), format_func=lambda i: f"第 {i+1} / {pages} 页", key="observation_audit_page")
    st.caption(f"符合条件 {len(rows)} 只；每页最多50只。完整证据见个股详情及冻结报告。")
    _table(st, [{"证券": r["symbol"], "名称": r["name"], "量价": status_label(r["technical_status"]),
        "资格": status_label(r["eligibility_status"]), "20日相对上证（百分点）": _number(r["metrics"].get("relative_return_20"), scale=100, suffix=" pp"),
        "待核查": _gap_labels(r) if r["eligibility_status"] == "pending" else r.get("company_risk_notice", "公司材料待查")}
        for r in rows[page*50:(page+1)*50]])
    st.caption(f"存在行情或资格缺口的股票：{len(report['data_gaps'])}只。未取得数据不等于没有风险。")


def render_dashboard(st, archive, output_root):
    """Render exactly one selected view; collapsed containers are not lazy views."""
    report = archive["report"]
    _summary(st, report)
    view = st.radio("阅读内容", ["今日总览", "个股详情", "筛选明细", "原文与证据", "导出"],
                    horizontal=True, key="observation_view", label_visibility="collapsed")
    if view == "今日总览":
        _overview(st, report)
    elif view == "个股详情":
        _detail(st, report)
    elif view == "筛选明细":
        _audit(st, report)
    elif view == "原文与证据":
        from .viewer import _label
        titles = [title for title, _ in report["sections"]]
        section = st.selectbox("报告章节", range(len(titles)), format_func=lambda i: _label(titles[i]), key="observation_section")
        st.subheader(_label(titles[section]))
        _blocks(st, report["sections"][section][1])
        st.caption("与本期冻结报告逐节对应；补采时间、风险、引用和来源限制均保留。")
    else:
        from .reports.observation import read_observation_report, EXPORTS
        st.subheader("导出本期观察日报")
        st.caption("下载原始冻结版本，包含完整研究依据、风险和核验明细。")
        # One fresh verification covers this render's complete registered bundle.
        checked = read_observation_report(output_root, archive["directory"])
        for name, (label, mime) in EXPORTS.items():
            st.download_button(label, checked["files"][name], file_name=report["trade_date"]+"-"+name,
                mime=mime, key="observation_export_"+name, on_click="ignore")
