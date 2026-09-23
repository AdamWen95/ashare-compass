"""Monotonic fake-clock checks; no real requests or waits."""
import copy

import pytest

import ashare_daily.research.sources as sources_module
from ashare_daily.research.sources import HttpResponse, collect_materials
from test_m3_sources import article, registry, source


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def collect(tmp_path, source, clock, transport, **changes):
    return collect_materials(registry_path=registry(tmp_path, source, timeout_seconds=15),
        start="2026-09-06T00:00:00+08:00", cutoff="2026-09-08T23:59:59+08:00",
        sample_symbols=[], output_dir=tmp_path / "out", transport=transport,
        sleep=clock.sleep, **changes)


def test_zero_budget_keeps_source_status_without_network(tmp_path, source, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(sources_module.time, "monotonic", clock.monotonic)
    result = collect(tmp_path, source, clock, lambda *args: pytest.fail("network forbidden"), max_seconds=0)
    assert result["request_count"] == 0 and result["runtime_limit_reached"] is True
    assert result["source_health"][0]["failures"] == ["source_runtime_limit"]
    assert result["status"] == "partial"


def test_timeout_reduced_to_remaining_and_collected_evidence_retained(tmp_path, source, monkeypatch):
    clock = Clock()
    source["pages"].append({"url": "https://example.org/second.html"})
    monkeypatch.setattr(sources_module.time, "monotonic", clock.monotonic)
    timeouts = []
    def transport(url, timeout):
        timeouts.append(timeout)
        if url.endswith("robots.txt"):
            clock.now += .1
            return HttpResponse(404, b"missing")
        clock.now += .75
        return HttpResponse(200, article())
    result = collect(tmp_path, source, clock, transport, max_seconds=1)
    assert timeouts == pytest.approx([1, .8])
    assert result["evidence_count"] == 1 and result["request_count"] == 2
    assert result["source_health"][0]["status"] == "partial"
    assert result["runtime_limit_reached"] is True
    assert clock.sleeps == [.1]  # Insufficient remaining time never shortens the next interval.
    assert result["elapsed_seconds"] == pytest.approx(.95)


def test_timeout_exhaustion_does_not_retry(tmp_path, source, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(sources_module.time, "monotonic", clock.monotonic)
    timeouts = []
    def transport(url, timeout):
        timeouts.append(timeout)
        clock.now += timeout
        raise TimeoutError("private timeout detail")
    result = collect(tmp_path, source, clock, transport, max_seconds=.4)
    assert timeouts == pytest.approx([.4])
    assert result["request_count"] == 1 and result["runtime_limit_reached"] is True
    assert not clock.sleeps


def test_no_new_source_starts_after_global_deadline(tmp_path, source, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(sources_module.time, "monotonic", clock.monotonic)
    path = registry(tmp_path, source)
    import json
    value = json.loads(path.read_text(encoding="utf-8"))
    second = copy.deepcopy(source)
    second["registration"].update(source_id="second", source_url="https://other.example.org")
    second["pages"] = [{"url": "https://other.example.org/article.html"}]
    value["sources"].append(second)
    path.write_text(json.dumps(value), encoding="utf-8")
    calls = []
    def transport(url, timeout):
        calls.append(url)
        clock.now += 1
        return HttpResponse(404, b"missing")
    result = collect_materials(registry_path=path, start="2026-09-06T00:00:00+08:00",
        cutoff="2026-09-08T23:59:59+08:00", sample_symbols=[], output_dir=tmp_path / "out",
        transport=transport, sleep=clock.sleep, max_seconds=1)
    assert calls == ["https://example.org/robots.txt"]
    assert all(row["status"] == "failed" for row in result["source_health"])
    assert all("source_runtime_limit" in row["failures"] for row in result["source_health"])


@pytest.mark.parametrize("limit", [-1, True, float("inf"), float("nan"), 14401])
def test_invalid_runtime_budget_rejected_before_network(tmp_path, source, limit):
    with pytest.raises(ValueError, match="material_runtime_limit_invalid"):
        collect(tmp_path, source, Clock(), lambda *args: pytest.fail("network forbidden"), max_seconds=limit)
