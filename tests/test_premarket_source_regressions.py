"""Independent source-review findings: structure, repeated evidence and deadlines."""
from datetime import datetime
import json
from pathlib import Path
import time

import pytest


@pytest.mark.parametrize("raw", [b'{"data": null}', b'[]', b'{"data": {"page": 1, "total": 1, "results": [null]}}'])
def test_csrc_malformed_shapes_are_explicit_parse_failures(raw):
    from ashare_daily.premarket.policy_sources import parse_csrc_index
    with pytest.raises(ValueError): parse_csrc_index(raw)


def test_first_observation_survives_new_run_and_content_revisions(tmp_path):
    from ashare_daily.premarket.source_http import SourceSession
    store = tmp_path / "observed.sqlite3"
    first = SourceSession(tmp_path / "one", max_requests=1, max_seconds=5, observation_database=store)
    second = SourceSession(tmp_path / "two", max_requests=1, max_seconds=5, observation_database=store)
    args = ("csrc", "https://www.csrc.gov.cn/csrc/c100028/c123/content.shtml")
    one = first.observe(*args, "a"*64, "2026-10-13T08:52:00+08:00")
    two = second.observe(*args, "a"*64, "2026-10-14T08:52:00+08:00")
    revised = second.observe(*args, "b"*64, "2026-10-14T08:52:00+08:00")
    assert two["first_seen_at"] == one["first_seen_at"] == "2026-10-13T08:52:00+08:00"
    assert two["previously_observed"] is True
    assert revised["first_seen_at"] == "2026-10-14T08:52:00+08:00"
    assert revised["previously_observed"] is False


def test_network_worker_total_deadline_cancels_even_continuous_output(tmp_path, monkeypatch):
    from ashare_daily.premarket import source_http
    worker = tmp_path / "slow.py"
    marker = tmp_path / "should-not-exist"
    worker.write_text("import sys,time,pathlib\nfor _ in range(40):\n sys.stdout.write('x');sys.stdout.flush();time.sleep(.05)\npathlib.Path(" + repr(str(marker)) + ").write_text('finished')\n")
    monkeypatch.setattr(source_http, "HTTP_WORKER", worker, raising=False)
    began = time.monotonic()
    with pytest.raises(TimeoutError): source_http._request("https://www.cninfo.com.cn/", None, .3, 100)
    assert time.monotonic() - began < 1.5
    assert not marker.exists()


def test_policy_collector_marks_repeated_version_as_background(tmp_path):
    from ashare_daily.premarket.policy_sources import collect_policy_sources
    from ashare_daily.premarket.observations import record_observation
    class Session:
        def __init__(self, day): self.day = day
        def observe(self, *args): return record_observation(tmp_path / "observations.sqlite3", *args)
        def get(self, url, sid, role):
            if role == "index":
                body = json.dumps({"data": {"page": 1, "total": 1, "results": [{"title": "某办法征求意见", "url": "//www.csrc.gov.cn/csrc/c100028/c123/content.shtml", "publishedTimeStr": "2026-10-12 16:59:43"}]}}).encode()
            else:
                body = '<meta name="ArticleTitle" content="某办法征求意见"><div class="info">日期：2026-10-12 来源：证监会</div><div class="detail-news"><p>该办法现向社会公开征求意见，履行相关程序后发布实施。</p></div>'.encode()
            return body, {"fetched_at": f"2026-10-{self.day}T08:52:00+08:00", "requested_at": f"2026-10-{self.day}T08:51:00+08:00", "raw_sha256": "a"*64}
    first = collect_policy_sources(Session(13), start="2026-10-10T08:55:00+08:00", cutoff="2026-10-13T08:55:00+08:00", directory=tmp_path, enabled={"csrc_regulation_news"})
    second = collect_policy_sources(Session(14), start="2026-10-10T08:55:00+08:00", cutoff="2026-10-14T08:55:00+08:00", directory=tmp_path, enabled={"csrc_regulation_news"})
    assert first["events"][0]["is_background"] is False
    assert second["events"][0]["is_background"] is True
    assert first["events"][0]["sources"][0]["first_seen_at"] == second["events"][0]["sources"][0]["first_seen_at"]


def test_robots_404_non_utf8_body_is_not_parsed_as_rules(tmp_path):
    from ashare_daily.premarket.source_http import SourceSession
    from ashare_daily.research.sources import HttpResponse
    session = SourceSession(tmp_path, max_requests=2, max_seconds=10, sleep=lambda _: None,
        transport=lambda url, *args: HttpResponse(404, b"\x81\xff") if url.endswith("robots.txt") else HttpResponse(200, b'{}', "application/json"))
    body, _ = session.get("https://www.cninfo.com.cn/new/data/szse_stock.json", "cninfo", "mapping")
    assert body == b'{}' and session.request_count == 2


def test_cninfo_midnight_timestamp_keeps_date_precision():
    from ashare_daily.premarket.announcements import parse_directory
    from test_premarket_local_sources import announcement
    row = announcement()
    row["announcementTime"] = int(datetime.fromisoformat("2026-10-13T00:00:00+08:00").timestamp()*1000)
    result = parse_directory({"totalRecordNum": 1, "hasMore": False, "announcements": [row]},
        {"code": "300872", "zwjc": "测试公司", "orgId": "opaque"})
    assert result["records"][0]["publication_precision"] == "date"
    assert result["records"][0]["published_at"] == "2026-10-13"
