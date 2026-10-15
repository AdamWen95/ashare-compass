"""Read-only morning research UI with edition freshness independent of data age."""
from datetime import datetime, time
from urllib.parse import urlsplit

from .premarket.contracts import SHANGHAI, aware
from .premarket.archive import _read


def edition_status(report, runs, *, now):
    now = aware(now)
    if report and report.get("report_date") == now.date().isoformat() and report.get("published_at"):
        if aware(report["published_at"]) <= now:
            return {"status": "ready", "message": "今日早报已发布" if report.get("publication_status") == "on_time" else "今日早报迟到发布，已保留实际时间"}
    today = [r for r in runs if r.get("report_date") == now.date().isoformat()
             and r.get("status") not in {"dry_run", "not_due", "reused"}]
    if today and today[0].get("status") == "running" and now.time() < time(9):
        return {"status": "preparing", "message": "今日早报正在准备，计划09:00发布"}
    if now.time() < time(9):
        return {"status": "pending", "message": "今日早报尚未发布；历史报告保留原日期"}
    return {"status": "missing_today", "message": "今日早报未生成或已超时；下方历史报告不代表今日已更新"}


def render_morning(st, archives, output_root, *, now=None, runs=None):
    from .viewer import _label
    now = aware(now or datetime.now(SHANGHAI))
    state = edition_status(archives[0]["report"] if archives else None, runs or [], now=now)
    (st.warning if state["status"] == "missing_today" else st.info)(state["message"])
    if not archives:
        st.caption("暂无可阅读的盘前简报。早报计划09:00发布；实际执行状态请查看任务记录。")
        return
    index = st.selectbox("选择盘前早报", range(len(archives)),
        format_func=lambda i: archives[i]["report"]["report_date"] + " · " + archives[i]["report"]["published_at"],
        key="premarket_edition")
    archive = _read(output_root.absolute(), archives[index]["directory"])
    report = archive["report"]
    st.subheader(report["title"])
    st.caption(f"早报 {report['report_date']} · 目标交易日 {report['target_session_date']} · 行情截至 {report['market_as_of_date']}")
    st.caption(f"消息截点 {report['news_cutoff_at']} · 实际发布 {report['published_at']}（北京时间）")
    stance = report["stance"]
    st.markdown("### 市场姿态：" + _label(stance["label"]))
    for reason in stance["reasons"]:
        st.write(reason)
    for fact in stance.get("analysis", []):
        st.write("现有证据：" + fact)
    for limitation in stance.get("limitations", []):
        st.caption("资料限制：" + limitation)
    if report["research_status"] != "ready":
        st.warning("资料覆盖尚不完整；以上按已有有效证据分析，缺失项可能改变结论。市场倾向与个股核查状态分别判断。")
    st.caption(report["notice"])
    st.subheader("隔夜重要变化")
    if not report["overnight_events"]:
        st.info("暂无已核验的新事件，不代表没有新闻或风险。")
    for event in report["overnight_events"]:
        st.write(event["text"])
        if event.get("is_background"):
            st.caption("此前已观察，本期作为背景保留")
        st.caption("来源发布记录，影响及生效条件尚待核验" if event.get("claim_type") == "publication_notice" else
            "原文支持的摘录，仍需核对语义" if event.get("validation_status") == "verified" else "研究推论，尚待核验")
        with st.expander("核对消息出处 · " + str(event["event_id"])):
            for citation in event.get("citations", []): st.text(citation["quote"])
            for source in event.get("sources", []):
                url = source.get("public_url", source.get("original_url", ""))
                parts = urlsplit(url)
                if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password:
                    st.link_button("查看原文", url)
                st.caption(str(source.get("published_at", "时间未知")))
                if source.get("locator_notice"):
                    st.caption(source["locator_notice"])
    st.subheader("重点关注方向")
    if not report["directions"]:
        st.info("本期没有完成核验的重点方向，保留空结果。")
    for direction in report["directions"]:
        with st.container(border=True):
            st.markdown("**" + _label(direction.get("name", direction["sector_id"])) + "**")
            st.write(direction["reason"])
            st.caption(direction["continuity"])
    for title, key in (("重点关注股票", "focus_candidates"), ("待核查观察", "pending_candidates")):
        st.subheader(title)
        if not report[key]:
            st.info("本期为空，不强行补足股票数量。")
        for row in report[key][:20]:
            with st.container(border=True):
                st.markdown("**" + _label(row.get("name", "") + " · " + row.get("symbol", "")) + "**")
                st.caption("技术评分 " + str(row.get("technical_score", "未取得")) + " · 不是上涨概率")
                for reason in row.get("reasons", []): st.write(reason)
                if row.get("reason") == "company_review_incomplete":
                    st.warning("公司公告或风险资料尚未完成核查")
                elif row.get("reason") == "market_not_attack":
                    st.caption("市场姿态不支持新增进攻，保留后续观察")
                st.caption(row.get("trade_status_notice", "本日交易状态待核验"))
                with st.expander("评分、风险与观察条件 · " + row["security_id"]):
                    st.table([{k: c.get(k) for k in ("label", "points", "reason")} for c in row.get("score_components", [])])
                    for text in row.get("watch_conditions", []): st.write("观察：" + text)
                    for text in row.get("invalidation_conditions", []): st.write("失效：" + text)
                    for text in row.get("company_review", {}).get("gaps", []): st.write("待查：" + text)
                    review = row.get("company_review", {})
                    for text in review.get("risk_flags", []): st.warning("公告风险定位：" + text)
                    for announcement in review.get("announcements", []):
                        st.write(announcement["title"])
                        st.caption(announcement["published_at"] + (" · 正文已取得，语义待核验" if announcement.get("body_status") == "extracted" else " · 正文未取得"))
                        url = announcement.get("original_url", "")
                        parts = urlsplit(url)
                        if parts.scheme == "https" and parts.hostname == "static.cninfo.com.cn" and not parts.username and not parts.password:
                            st.link_button("查看公司公告", url)
                    for text in review.get("financial_risk_flags", []): st.warning("财务风险：" + text)
                    for text in review.get("financial_gaps", []): st.write("财务待查：" + text)
                    if review.get("financial_facts"):
                        st.caption("财务原始事实与来源，保留接口口径")
                        st.json(review["financial_facts"])
                        st.json(review.get("financial_provenance", {}))
    with st.expander("完整筛选明细"):
        labels = {"pass": "通过", "fail": "未通过", "pending": "待核查", "unknown": "数据不足"}
        st.dataframe([{"证券": r.get("symbol"), "名称": r.get("name"),
            "量价条件": labels.get(r.get("technical_status"), "数据不足"),
            "基本资格": labels.get(r.get("eligibility_status"), "待核查"),
            "待查事项": "；".join(r.get("eligibility_gaps", []))} for r in report.get("screening_audit", [])], hide_index=True)
    with st.expander("数据覆盖与规则版本"):
        st.json(report["coverage"])
        st.json(report["rule_versions"])
        st.caption("初版规则为研究启发式；技术验证不代表已证明策略效果。")
    with st.expander("导出本期报告"):
        from .reports.m3_render import render_sections_html
        from .premarket.report import report_sections
        st.caption("HTML采用与网站一致的新版排版；本期事实、时间和风险保持不变，其他格式保留原始内容。")
        labels = {"premarket_brief.json": "JSON", "premarket_brief.md": "Markdown", "premarket_brief.html": "HTML"}
        for name, label in labels.items():
            data = archive["files"][name]
            if name == "premarket_brief.html":
                data = render_sections_html(report["title"], report["report_date"], report["notice"], report_sections(report),
                    eyebrow="盘前研究 · 3–10个交易日 · 数据与风险可追溯").encode("utf-8")
            st.download_button(label, data, file_name=report["report_date"] + "-" + name, key="premarket_download_" + name)
