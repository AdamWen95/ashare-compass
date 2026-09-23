from copy import deepcopy
import json
from pathlib import Path

import pytest

from ashare_daily.sector_pipeline import load_config, run_sector, archive_selection, read_selection, report_readiness
from ashare_daily.sector_selection import digest
from test_sector_selection import inputs, calculate

ROOT = Path(__file__).resolve().parents[1]


def setup(tmp_path, count=105):
    config = json.loads((ROOT / "config/sector_first.json").read_text(encoding="utf-8"))
    config.update(output_directory="outputs/offline_test/sector_first", database="data/offline_test/market.sqlite3")
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    data = inputs(count)
    selection, rows = calculate(data)
    selection["config_hash"] = digest(config)
    selection.pop("selection_id")
    selection.pop("content_hash")
    selection["content_hash"] = digest(selection)
    selection["selection_id"] = "sector-2026-09-11-"+selection["content_hash"][:20]
    packet = dict(zip(("universe", "catalog", "memberships", "quotes"), data))
    packet["calendar"] = {"calendar_verified": True, "calendar": {"2026-09-11": True}}
    return config, selection, rows, packet


def test_dry_run_no_network_no_database_no_artifacts(tmp_path, monkeypatch):
    config, *_ = setup(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("dry run called source")
    monkeypatch.setattr("ashare_daily.sector_pipeline.collect_inputs", forbidden)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    result, code = run_sector(root=tmp_path, config_path="config.json", target="2026-09-11", dry_run=True)
    assert result["network_requests"] == 0 and result["model_calls"] == 0 and code == 0
    assert before == sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))


def test_archive_repeat_is_byte_identical_and_new_versions_not_overwrite(tmp_path):
    config, selection, rows, packet = setup(tmp_path)
    directory = archive_selection(tmp_path, config, selection, rows, packet)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    archive_selection(tmp_path, config, selection, rows, packet)
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(ValueError, match="immutable"):
        archive_selection(tmp_path, config, selection, rows, {**packet, "bad": True})
    assert (directory / "sector_selection.json").read_bytes() == before["sector_selection.json"]


def test_test_selection_cannot_resume_into_research(tmp_path):
    config, selection, rows, packet = setup(tmp_path)
    archive_selection(tmp_path, config, selection, rows, packet)
    with pytest.raises(ValueError, match="test_selection"):
        read_selection(tmp_path, config, selection["selection_id"])


def test_report_denominator_is_selection_not_universe_and_risk_not_green(tmp_path):
    config, selection, rows, packet = setup(tmp_path)
    selection["universe_count"] = 6000
    archive_selection(tmp_path, config, selection, rows, packet)
    result = report_readiness(tmp_path, config, selection, {"status": "technical_ready", "history_ready_count": 105,
        "adjustment_ready_count": 105, "metrics": {"history_requests_outside_selection": 0}})
    assert result["status"] == "technical_ready_risk_pending"
    assert result["counts"]["selected_securities"] == 105
    assert result["not_requested_by_design"] == 5895
    assert result["full_market_history_complete"] is False
    assert result["formal_qualification_verified"] is False
    assert result["model_calls"] == 0
    assert "资金净流入" in Path(result["report_path"]).read_text(encoding="utf-8")  # explicit denial
    manifest = json.loads((Path(result["report_path"]).parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "f2s1-readiness-manifest-v1"


def test_zero_and_blocked_preserve_zero_tasks_with_distinct_reason(tmp_path):
    config, selection, rows, packet = setup(tmp_path)
    selection.update(members=[], selected_security_count=0, selected_count=0, selected_sectors=[],
                     selection_status="selection_blocked", selection_verified=False)
    directory = archive_selection(tmp_path, config, selection, rows, packet)
    plan = json.loads((directory / "history_fetch_plan.json").read_text(encoding="utf-8"))
    assert plan["status"] == "selection_blocked" and plan["tasks"] == []
    result = report_readiness(tmp_path, config, selection)
    assert result["status"] == "selection_blocked"
    assert result["counts"]["selected_securities"] == 0


@pytest.mark.parametrize("field,value", [("market_scope", "all_a"), ("research_mode", "full_market"),
    ("target_trading_days", 120), ("model_calls", 1), ("themes_enabled", True), ("database", "../outside.sqlite3")])
def test_config_scope_and_budget_cannot_leak(tmp_path, field, value):
    config, *_ = setup(tmp_path)
    config[field] = value
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(tmp_path, "config.json")


@pytest.mark.parametrize("field,value", [("enabled", False), ("user_authorized", False),
    ("permission_status", "unconfirmed"), ("permitted_storage", False),
    ("permitted_automated_access", False), ("llm_export", True)])
def test_current_permission_reversal_blocks_frozen_authorization(tmp_path, field, value):
    config, *_ = setup(tmp_path)
    config["sina"][field] = value
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    result, code = run_sector(root=tmp_path, config_path="config.json", operation="prepare", selection_id="unused")
    assert result["status"] == "permission_required" and code == 2
    assert result["network_requests"] == 0


@pytest.mark.parametrize("evidence_valid", [True, False])
def test_reparse_archive_revalidates_existing_status_overlay_before_restoring_it(tmp_path, monkeypatch, evidence_valid):
    """Exercise integration only; HTTP and status contracts have separate fixtures."""
    from dataclasses import asdict
    from ashare_daily.sector_pipeline import revalidate_dated_quote_archive
    from test_sina_sectors import identity, quote, response, QUOTE_BASE

    member = asdict(identity())
    universe = {"members": [member], "mode": "offline_test"}
    directory = tmp_path / "offline_test"
    directory.mkdir()
    http_path = directory / "quote-http.json"
    http_path.write_text(json.dumps(response(QUOTE_BASE + "sh600001", quote(price="0"))), encoding="utf-8")
    status_path = directory / "status-result.json"
    packet = {"rows": [{"symbol": "sh600001", "full_day_halt_evidence": {"untrusted_old_summary": True}}],
        "evidence": [{"path": str(http_path), "sha256": "fixture-boundary-isolated"}],
        "status_evidence_overlays": [{"result_path": str(status_path), "applied_security_ids": [member["security_id"]]}]}
    original = deepcopy(packet)
    checked, applied = [], []
    monkeypatch.setattr("ashare_daily.sector_pipeline.verify_http_evidence", lambda root, packets: checked.append(packets))

    def revalidate_status(root, actual_universe, parsed, target, path):
        assert actual_universe is universe and target == "2026-09-11" and path == status_path
        assert "status_evidence_overlays" not in parsed
        assert "full_day_halt_evidence" not in parsed["rows"][0]
        applied.append(path)
        if not evidence_valid:
            raise ValueError("original_status_evidence_changed")
        result = deepcopy(parsed)
        result["rows"][0]["full_day_halt_evidence"] = {"verified_original_source": True}
        result["status_evidence_overlays"] = [{"result_path": str(path), "revalidated": True}]
        return result

    monkeypatch.setattr("ashare_daily.providers.sector_status.apply_status_evidence", revalidate_status)
    if evidence_valid:
        result = revalidate_dated_quote_archive(tmp_path, packet, universe, "2026-09-11")
        assert result["rows"][0]["full_day_halt_evidence"] == {"verified_original_source": True}
        assert result["status_evidence_overlays"][0]["revalidated"] is True
        assert result["rows"][0]["tradestatus"] is None
        assert result["network_requests_this_validation"] == 0
    else:
        with pytest.raises(ValueError, match="original_status_evidence_changed"):
            revalidate_dated_quote_archive(tmp_path, packet, universe, "2026-09-11")
    assert len(checked) == 1 and applied == [status_path]
    assert packet == original
