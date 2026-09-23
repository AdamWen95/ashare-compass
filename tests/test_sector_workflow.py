from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ashare_daily.sector_workflow import screen_frozen, _read_observation
from test_sector_selection import inputs, calculate

ROOT = Path(__file__).resolve().parents[1]


def empty_setup(tmp_path, monkeypatch):
    data = inputs(2)
    for row in data[3]["rows"]:
        row.update(change_pct=-2, close=9.8)
    selection, _ = calculate(data)
    config = json.loads((ROOT / "config/sector_first.json").read_text(encoding="utf-8"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/sector_screening.json").write_bytes((ROOT / "config/sector_screening.json").read_bytes())
    monkeypatch.setattr("ashare_daily.sector_workflow._selection", lambda *args, **kwargs: (deepcopy(selection), deepcopy(config)))
    monkeypatch.setattr("ashare_daily.sector_workflow._output", lambda *args: tmp_path / "outputs/offline_test/f3s")
    def forbidden(*args, **kwargs):
        pytest.fail("empty production scope attempted a history or benchmark operation")
    monkeypatch.setattr("ashare_daily.sector_history.screening_history_inputs", forbidden)
    monkeypatch.setattr("ashare_daily.sector_benchmark.read_benchmark", forbidden)
    return selection


def test_zero_scope_real_core_is_na_and_repeated_screen_reuses_exact_frozen_result(tmp_path, monkeypatch):
    selection = empty_setup(tmp_path, monkeypatch)
    first, code = screen_frozen(tmp_path, selection["selection_id"])
    assert code == 0 and first["status"] == "not_applicable"
    assert first["counts"]["stock_count"] == 0 and first["counts"]["candidate_count"] == 0
    result = json.loads(Path(first["json_path"]).read_text(encoding="utf-8"))
    assert result["technical_computable_ratio"] is None and result["denominator_zero_display"] == "N/A"
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    second, _ = screen_frozen(tmp_path, selection["selection_id"])
    assert second["reused_evaluation"] is True and second["evaluation_id"] == first["evaluation_id"]
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert not (tmp_path / "data").exists()


def test_report_hash_and_purpose_cannot_be_rewritten_in_place(tmp_path, monkeypatch):
    selection = empty_setup(tmp_path, monkeypatch)
    result, _ = screen_frozen(tmp_path, selection["selection_id"])
    Path(result["csv_path"]).write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        screen_frozen(tmp_path, selection["selection_id"], report_only=True)


def test_incomplete_manifest_is_rejected_even_with_valid_remaining_files(tmp_path, monkeypatch):
    selection = empty_setup(tmp_path, monkeypatch)
    result, _ = screen_frozen(tmp_path, selection["selection_id"])
    directory = Path(result["json_path"]).parent
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"].pop("screening_inputs.json")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        _read_observation(directory, selection["selection_id"])


def test_missing_report_does_not_trigger_screen_or_history(tmp_path, monkeypatch):
    selection = empty_setup(tmp_path, monkeypatch)
    result, code = screen_frozen(tmp_path, selection["selection_id"], report_only=True)
    assert code == 2 and result["status"] == "screening_not_run"
    assert not (tmp_path / "outputs").exists()


def test_dry_run_does_not_create_snapshot_or_request_data(tmp_path, monkeypatch):
    selection = empty_setup(tmp_path, monkeypatch)
    before = {str(p) for p in tmp_path.rglob("*")}
    result, code = screen_frozen(tmp_path, selection["selection_id"], dry_run=True)
    assert code == 0 and result["network_requests"] == 0
    assert before == {str(p) for p in tmp_path.rglob("*")}


def test_benchmark_dry_run_does_not_prepare_even_cached_artifacts(tmp_path, monkeypatch):
    from ashare_daily.sector_workflow import run_f3s
    monkeypatch.setattr("ashare_daily.sector_workflow.read_validation", lambda *args, **kwargs: ({"selection_id": "validation-only"}, {}))
    def forbidden(*args, **kwargs):
        pytest.fail("benchmark dry-run tried to prepare/write")
    monkeypatch.setattr("ashare_daily.sector_benchmark.prepare_benchmark", forbidden)
    args = SimpleNamespace(sector_command="validation-benchmark", selection="validation-only", validation_config="unused",
                           dry_run=True, online=True, max_seconds=30)
    result, code = run_f3s(tmp_path, args)
    assert code == 0 and result["status"] == "dry_run" and result["network_requests"] == 0
    assert not list(tmp_path.iterdir())
