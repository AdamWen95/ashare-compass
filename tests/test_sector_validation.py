from copy import deepcopy
import json
from pathlib import Path

import pytest

from ashare_daily.sector_validation import freeze_validation, read_validation, validation_config
from ashare_daily.sector_selection import digest, verify_selection
from ashare_daily.sector_pipeline import run_sector
from test_sector_selection import inputs, calculate

ROOT = Path(__file__).resolve().parents[1]


def fixture(tmp_path, monkeypatch, count=137):
    (tmp_path / "config").mkdir()
    for name in ("sector_validation.json", "sector_first.json"):
        (tmp_path / "config" / name).write_bytes((ROOT / "config" / name).read_bytes())
    data = inputs(count, 2)
    for quote in data[3]["rows"]:
        quote.update(change_pct=-2, close=9.8)
    parent, _ = calculate(data)
    parent.update(mode="research", universe_board_counts={})
    parent.pop("content_hash")
    parent.pop("selection_id")
    parent["content_hash"] = digest(parent)
    parent["selection_id"] = "sector-2026-09-11-" + parent["content_hash"][:20]
    source = dict(zip(("universe", "catalog", "memberships", "quotes"), data))
    source["calendar"] = {"calendar_verified": True, "calendar": {"2026-09-11": True}}
    parent_dir = tmp_path / "outputs/research/sse_szse_a/sector_first" / parent["selection_id"]
    parent_dir.mkdir(parents=True)
    (parent_dir / "source_inputs.json").write_text(json.dumps(source), encoding="utf-8")
    # Only the parent I/O boundary is replaced. All output remains in pytest's
    # isolated temporary root; these fixtures are never online evidence.
    monkeypatch.setattr("ashare_daily.sector_validation.read_selection", lambda *args, **kwargs: (deepcopy(parent), {}))
    return parent, parent_dir


def test_full_multi_member_engineering_scope_and_production_zero_unchanged(tmp_path, monkeypatch):
    parent, directory = fixture(tmp_path, monkeypatch)
    before = (directory / "source_inputs.json").read_bytes()
    result = freeze_validation(tmp_path, parent["selection_id"])
    assert result["counts"]["validation_securities"] == 137
    assert result["sector_id"] == "industry-0"
    frozen, _ = read_validation(tmp_path, result["selection_id"])
    assert len(frozen["members"]) == 137
    assert {m["board"] for m in frozen["members"]} == {"sse_main", "szse_main", "chinext", "star"}
    assert frozen["automatic_selection"] is False and frozen["production_eligible"] is False
    assert frozen["source_selection_counts"]["selected_security_count"] == 0
    assert (directory / "source_inputs.json").read_bytes() == before
    assert parent["members"] == []
    assert not (tmp_path / "data/research/market.sqlite3").exists()


def test_validation_dry_run_never_creates_data_or_selection(tmp_path, monkeypatch):
    parent, _ = fixture(tmp_path, monkeypatch)
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    result = freeze_validation(tmp_path, parent["selection_id"], dry_run=True)
    assert result["network_requests"] == 0
    assert before == sorted(str(p) for p in tmp_path.rglob("*"))


@pytest.mark.parametrize("key,value", [("database", "data/research/market.sqlite3"),
    ("output_directory", "outputs/research/sector_first"), ("history_lock_file", "data/research/lock"),
    ("purpose", "production"), ("production_eligible", True)])
def test_validation_target_or_purpose_cannot_escape(tmp_path, monkeypatch, key, value):
    fixture(tmp_path, monkeypatch)
    path = tmp_path / "config/sector_validation.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config[key] = value
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        validation_config(tmp_path)


def test_validation_never_routes_through_production_daily(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="production_rejects"):
        run_sector(root=tmp_path, config_path="config/sector_validation.json", target="2026-09-11", dry_run=True)


def test_frozen_validation_source_and_member_mutations_are_rejected(tmp_path, monkeypatch):
    parent, directory = fixture(tmp_path, monkeypatch)
    result = freeze_validation(tmp_path, parent["selection_id"])
    path = Path(result["json_path"])
    frozen = json.loads(path.read_text(encoding="utf-8"))
    bad = deepcopy(frozen)
    bad["members"].pop()
    with pytest.raises(ValueError, match="hash"):
        verify_selection(bad)
    (directory / "source_inputs.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="original_source_hash"):
        read_validation(tmp_path, result["selection_id"])


def test_later_validation_freeze_keeps_parent_cutoff_and_reconstruction(tmp_path, monkeypatch):
    parent, _ = fixture(tmp_path, monkeypatch)
    result = freeze_validation(tmp_path, parent["selection_id"])
    frozen, _ = read_validation(tmp_path, result["selection_id"])
    assert frozen["historical_reconstruction"] is True
    assert frozen["source_cutoff_at"] == parent["cutoff_at"]
    assert frozen["cutoff_at"] != parent["cutoff_at"]
