from datetime import datetime

from streamlit.testing.v1 import AppTest

from test_premarket_report import make_brief, DUE
from premarket_ui_helpers import morning_content


def test_today_state_distinguishes_old_report_from_current_success():
    from ashare_daily.viewer_premarket import edition_status
    report = make_brief()
    report["published_at"] = DUE.isoformat()
    report["publication_status"] = "on_time"
    assert edition_status(report, [], now=DUE)["status"] == "ready"
    assert edition_status(report, [], now=datetime.fromisoformat("2026-10-13T09:00:00+08:00"))["status"] == "missing_today"
    assert edition_status(None, [{"report_date": "2026-10-12", "status": "running"}],
                          now=datetime.fromisoformat("2026-10-12T08:40:00+08:00"))["status"] == "preparing"


def test_morning_reader_renders_posture_pending_and_exports(tmp_path):
    from ashare_daily.premarket.archive import publish_brief
    publish_brief(tmp_path, make_brief(), now=DUE)
    script = f'''
from datetime import datetime
from pathlib import Path
import streamlit as st
from ashare_daily.premarket.archive import scan_briefs
from ashare_daily.viewer_premarket import render_morning
now = datetime.fromisoformat("2026-10-12T09:00:00+08:00")
root = Path({str(tmp_path / "outputs")!r})
archives, errors = scan_briefs(root, now=now)
render_morning(st, archives, root, now=now, runs=[])
'''
    app = AppTest.from_string(script).run()
    assert not app.exception
    text = ' '.join(morning_content(app).parts)
    assert "进攻倾向" in text and "资料限制" in text
    assert "按兵不动的原因可能是资料不足" not in text
    assert "待核查" in text
    assert "2026-10-09" in text
    assert '完整筛选明细' in text
    assert len(app.get("download_button")) == 3


def test_empty_reader_has_explicit_state_without_dummy_stocks(tmp_path):
    script = f'''
from datetime import datetime
from pathlib import Path
import streamlit as st
from ashare_daily.viewer_premarket import render_morning
render_morning(st, [], Path({str(tmp_path)!r}), now=datetime.fromisoformat("2026-10-12T09:00:00+08:00"), runs=[])
'''
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert any("未生成" in e.value for e in app.warning)


def test_main_reader_masthead_identifies_morning_edition(tmp_path):
    from ashare_daily.premarket.archive import publish_brief
    publish_brief(tmp_path, make_brief(), now=DUE)
    script = f'''
from pathlib import Path
from ashare_daily.viewer import render_app
render_app(Path({str(tmp_path / "outputs")!r}))
'''
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert any("<h1>盘前研究简报</h1>" in e.value for e in app.markdown)
