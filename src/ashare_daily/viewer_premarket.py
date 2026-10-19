"""Read-only morning research UI with edition freshness independent of data age."""
from datetime import datetime, time

from .premarket.contracts import SHANGHAI, aware
from .premarket.archive import _read
from .premarket.presentation import render_body, render_document
from .premarket.presentation_style import STYLE


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
    st.html(STYLE)
    st.html(render_body(report))
    with st.container(key="premarket_export"):
        with st.expander("导出本期报告"):
            st.caption("HTML采用与网站一致的新版排版；本期事实、时间和风险保持不变，其他格式保留原始内容。")
            labels = {"premarket_brief.json": "JSON", "premarket_brief.md": "Markdown", "premarket_brief.html": "HTML"}
            for name, label in labels.items():
                data = render_document(report).encode("utf-8") if name.endswith(".html") else archive["files"][name]
                st.download_button(label,data,file_name=report["report_date"]+"-"+name,key="premarket_download_"+name)
