from datetime import datetime

import pytest

from test_m3_sources import article, registry, source
from ashare_daily.research.sources import HttpResponse


def test_required_roles_and_truncated_directory_cannot_look_complete():
    from ashare_daily.premarket.source_policy import coverage_summary
    reg = {"sources": [{"registration": {"source_id": "s", "enabled": True}, "research_roles": ["macro_policy"]}]}
    collection = {"source_health": [{"source_id": "s", "status": "ok", "failures": []}]}
    result = coverage_summary(reg, collection, required_roles=["macro_policy", "securities_regulation"])
    assert result["ready"] is False
    assert result["roles"]["macro_policy"]["status"] == "ready"
    assert result["roles"]["securities_regulation"]["status"] == "not_enabled"
    collection["source_health"][0]["index_limit_reached"] = True
    assert coverage_summary(reg, collection, required_roles=["macro_policy"])["ready"] is False


def test_pending_cutoff_collects_before_deadline_without_claiming_future_observation(tmp_path, source, monkeypatch):
    from ashare_daily.research import sources
    stamp = [datetime.fromisoformat("2026-10-12T08:50:00+08:00")]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return stamp[0]
    monkeypatch.setattr(sources, "datetime", Clock)
    def get(url, timeout):
        stamp[0] = datetime.fromisoformat("2026-10-12T08:54:00+08:00")
        return HttpResponse(404, b"") if url.endswith("robots.txt") else HttpResponse(200, article(published="2026-10-12 08:52"))
    result = sources.collect_materials(registry_path=registry(tmp_path, source),
        start="2026-10-11T08:55:00+08:00", cutoff="2026-10-12T08:55:00+08:00", sample_symbols={},
        output_dir=tmp_path / "out", transport=get, sleep=lambda _: None, max_seconds=300, pending_cutoff=True)
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["fetched_at"] == "2026-10-12T08:54:00+08:00"


def test_pending_cutoff_is_not_arbitrary_future_window(tmp_path, source):
    from ashare_daily.research import sources
    with pytest.raises(ValueError):
        sources.collect_materials(registry_path=registry(tmp_path, source), start="2098-01-01T00:00:00+08:00",
            cutoff="2099-01-01T00:00:00+08:00", sample_symbols={}, output_dir=tmp_path / "out",
            max_seconds=300, pending_cutoff=True)


def test_ambiguous_current_news_time_downgrades_coverage():
    from ashare_daily.premarket.source_policy import coverage_summary
    reg = {"sources": [{"registration": {"source_id": "s", "enabled": True}, "research_roles": ["macro_policy"]}]}
    collection = {"source_health": [{"source_id": "s", "status": "empty_bounded_window", "failures": []}],
                  "rejected": [{"source_id": "s", "reason": "after_cutoff_or_date_precision_ambiguous"}]}
    assert coverage_summary(reg, collection, required_roles=["macro_policy"])["ready"] is False
