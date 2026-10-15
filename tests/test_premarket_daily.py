from datetime import datetime
import json
from pathlib import Path
import shutil

from test_premarket_timing import CALENDAR
from test_premarket_market import snapshot, market_inputs
from test_premarket_candidates import preselect

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self, stamp="2026-10-12T08:30:00+08:00"):
        self.value = datetime.fromisoformat(stamp)
    def now(self): return self.value
    def sleep(self, seconds):
        from datetime import timedelta
        self.value += timedelta(seconds=seconds)


class Services:
    def __init__(self, clock): self.clock, self.calls = clock, []
    def calendar(self, now): return CALENDAR
    def market(self, schedule, directory, seconds, run_id):
        self.calls.append("market")
        self.clock.value = datetime.fromisoformat("2026-10-12T08:50:00+08:00")
        market = snapshot(market_inputs())
        market["market_as_of_date"] = market["benchmark"]["market_as_of_date"] = schedule["market_as_of_date"]
        return {"market": market, "preselection": preselect(), "company_reviews": {}, "entities": {}}
    def news(self, schedule, window, prepared, directory, seconds):
        self.calls.append("news")
        self.clock.value = datetime.fromisoformat("2026-10-12T08:55:00+08:00")
        return {"coverage": {"ready": False, "roles": {}}, "evidence": [], "company_reviews": {}}
    def interpret(self, news, prepared, schedule, directory, run_id, seconds, skip_model):
        self.calls.append("interpret")
        return {"events": [], "model_run": {"call_count": 0, "status": "skipped"}}


def setup(tmp_path):
    from ashare_daily.operations.daily import DailyConfig
    (tmp_path / "config").mkdir()
    for path in (ROOT / "config").glob("*.json"):
        shutil.copyfile(path, tmp_path / "config" / path.name)
    cfg = DailyConfig.model_validate_json((tmp_path / "config/premarket_daily.json").read_text(encoding="utf-8"))
    policy = json.loads((tmp_path / cfg.premarket_config).read_text(encoding="utf-8"))
    policy["production_enabled"] = True
    (tmp_path / cfg.premarket_config).write_text(json.dumps(policy), encoding="utf-8")
    return cfg


def invoke(root, cfg, services, clock, **kwargs):
    from ashare_daily.operations.premarket import run_premarket_daily
    return run_premarket_daily(project=root, config=cfg, config_path="config/premarket_daily.json",
        target=None, cutoff=None, start=None, now=clock.now(), dry_run=kwargs.pop("dry_run", False),
        scheduled=True, planned=None, services=services, clock=clock, **kwargs)


def test_dry_run_has_no_collection_and_correct_three_dates(tmp_path):
    cfg = setup(tmp_path)
    clock = Clock()
    services = Services(clock)
    result = invoke(tmp_path, cfg, services, clock, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["market_as_of_date"] == "2026-10-09"
    assert not services.calls


def test_bounded_pipeline_publishes_at_nine_and_reuses_without_requests(tmp_path):
    cfg = setup(tmp_path)
    clock = Clock()
    services = Services(clock)
    first = invoke(tmp_path, cfg, services, clock)
    assert first["generation_status"] == "ok"
    assert first["ended_at"] == "2026-10-12T09:00:00+08:00"
    assert first["status"] == "partial"
    before = list(services.calls)
    second = invoke(tmp_path, cfg, services, clock)
    assert second["status"] == "reused"
    assert services.calls == before


def test_after_0915_no_new_calls(tmp_path):
    cfg = setup(tmp_path)
    clock = Clock("2026-10-12T09:15:00+08:00")
    services = Services(clock)
    result = invoke(tmp_path, cfg, services, clock)
    assert result["status"] == "missed_window"
    assert not services.calls


def test_release_disabled_cannot_start_external_work(tmp_path):
    cfg = setup(tmp_path)
    policy_path = tmp_path / cfg.premarket_config
    policy = json.loads(policy_path.read_text())
    policy["production_enabled"] = False
    policy_path.write_text(json.dumps(policy))
    clock = Clock()
    services = Services(clock)
    assert invoke(tmp_path, cfg, services, clock)["status"] == "release_gate_blocked"
    assert services.calls == []


def test_old_workflow_attempts_count_toward_same_day_limit(tmp_path):
    from ashare_daily.operations.daily import atomic_json
    cfg = setup(tmp_path)
    for i in range(2):
        atomic_json(tmp_path / cfg.output_directory / "runs" / f"old{i}" / "result.json", {
            "schema_version": "m4-run-v1", "run_id": f"old{i}", "scope": "sse_szse_a", "research_mode": "sector_first",
            "status": "failed", "trigger_kind": "scheduled", "started_at": "2026-10-12T07:00:00+08:00",
            "target_trade_date": "2026-10-09", "workflow_version": "sector-observation-daily-v1"})
    clock = Clock()
    services = Services(clock)
    result = invoke(tmp_path, cfg, services, clock)
    assert result["status"] == "catchup_limit"
    assert not services.calls


def test_late_run_publishes_one_edition_only(tmp_path, monkeypatch):
    from ashare_daily.operations import premarket
    cfg = setup(tmp_path)
    clock = Clock("2026-10-12T09:02:00+08:00")
    services = Services(clock)
    real = premarket.publish_brief
    def capture(*args, **kwargs):
        result = real(*args, **kwargs)
        clock.sleep(1)
        return result
    monkeypatch.setattr(premarket, "publish_brief", capture)
    result = invoke(tmp_path, cfg, services, clock)
    assert result["generation_status"] == "ok"
    from ashare_daily.premarket.archive import scan_briefs
    assert len(scan_briefs(tmp_path / "outputs", now=clock.now())[0]) == 1
