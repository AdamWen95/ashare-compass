"""Readable exports preserve frozen facts and never rewrite historical artifacts."""
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from ashare_daily.reports.m3_render import render_sections_html


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.ids, self.unsafe = [], [], []
        self.article_depth, self.article_text = 0, []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "article":
            self.article_depth += 1
        if "id" in values:
            self.ids.append(values["id"])
        if tag == "a":
            self.links.append(values.get("href"))
        if tag in {"script", "iframe", "img"} or any(key.startswith("on") for key in values):
            self.unsafe.append(tag)

    def handle_endtag(self, tag):
        if tag == "article":
            self.article_depth -= 1

    def handle_data(self, text):
        if self.article_depth:
            self.article_text.append(text)


def test_navigation_handles_duplicate_untrusted_headings_without_changing_facts():
    payload = '<img src="https://example.invalid/pixel" onerror="bad()">'
    sections = [(payload, [("paragraph", "资料限制：消息覆盖不完整"), ("subheading", "观察对象A"),
        ("paragraph", "技术评分：65"), ("table", (["指标", "原始值"], [["未知项", "未取得"], ["收益", "-1.25%"]]))]),
        (payload, [("paragraph", "重复标题下的另一条事实")])]
    before = deepcopy(sections)
    rendered = render_sections_html("研究报告", "2026-10-15", "只读研究", sections)
    parsed = Document()
    parsed.feed(rendered)
    assert "#section-1" in parsed.links and "#section-2" in parsed.links
    assert len(parsed.ids) == len(set(parsed.ids))
    assert all(link.startswith("#") and link[1:] in parsed.ids for link in parsed.links)
    assert not parsed.unsafe and payload not in rendered and "&lt;img" in rendered
    assert "未取得" in rendered and "-1.25%" in rendered and "技术评分：65" in rendered
    assert sections == before


def test_watchlist_group_labels_do_not_become_part_of_previous_stock_card():
    sections = [("关注股票与风险", [("paragraph", "重点关注"), ("subheading", "公司A"),
        ("paragraph", "公司A的依据"), ("paragraph", "待核查观察"), ("subheading", "公司B"),
        ("paragraph", "公司B的资料缺口"), ("paragraph", "辅助策略限制：尚未验证胜率"),
        ("paragraph", "参考源码版本：固定版本")])]
    parsed = Document()
    parsed.feed(render_sections_html("报告", "2026-10-15", "只读", sections))
    assert "公司A的依据" in parsed.article_text and "公司B的资料缺口" in parsed.article_text
    assert "重点关注" not in parsed.article_text and "待核查观察" not in parsed.article_text
    assert "辅助策略限制：尚未验证胜率" not in parsed.article_text
    assert "参考源码版本：固定版本" not in parsed.article_text


@pytest.mark.parametrize("kind", ["observation", "premarket"])
def test_html_download_uses_current_layout_and_preserves_frozen_bundle(tmp_path, monkeypatch, kind):
    if kind == "observation":
        from test_observation_daily import publish
        result = publish(tmp_path, [("pass", "pass")])
        filename = "daily_observation.html"
        key = "observation_export_" + filename
        script = f'''
from pathlib import Path
from ashare_daily.reports.observation import read_observation_report
from ashare_daily.viewer_dashboard import render_dashboard
import streamlit as st
root = Path({str(tmp_path / 'outputs')!r})
archive = read_observation_report(root, Path({str(result['directory'])!r}))
render_dashboard(st, archive, root)
'''
    else:
        from test_premarket_report import make_brief, DUE
        from ashare_daily.premarket.archive import publish_brief
        result = publish_brief(tmp_path, make_brief(), now=DUE)
        filename = "premarket_brief.html"
        key = "premarket_download_" + filename
        script = f'''
from datetime import datetime
from pathlib import Path
from ashare_daily.premarket.archive import scan_briefs
from ashare_daily.viewer_premarket import render_morning
import streamlit as st
root = Path({str(tmp_path / 'outputs')!r})
now = datetime.fromisoformat({DUE.isoformat()!r})
archives, errors = scan_briefs(root, now=now)
render_morning(st, archives, root, now=now, runs=[])
'''
    directory = Path(result["directory"])
    original = b"<!doctype html><html><body>LEGACY FROZEN EXPORT</body></html>"
    (directory / filename).write_bytes(original)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][filename] = hashlib.sha256(original).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = {p: p.read_bytes() for p in directory.iterdir()}
    downloads = {}
    real_download = st.download_button

    def capture(label, data, *args, **kwargs):
        downloads[kwargs["key"]] = data
        return real_download(label, data, *args, **kwargs)

    monkeypatch.setattr(st, "download_button", capture)
    app = AppTest.from_string(script).run()
    if kind == "observation":
        app.radio(key="observation_view").set_value("导出").run()
    assert not app.exception
    body = downloads[key]
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    assert ('今日方向简报' if kind=='premarket' else '方向研究室') in text and "LEGACY FROZEN EXPORT" not in text
    assert "class=\"report-nav\"" in text
    assert {p: p.read_bytes() for p in directory.iterdir()} == before
    # Non-HTML artifacts remain byte-for-byte exports of the verified archive.
    for name, content in before.items():
        exported_key = ("observation_export_" if kind == "observation" else "premarket_download_") + name.name
        if name.name != filename and exported_key in downloads:
            assert downloads[exported_key] == content
