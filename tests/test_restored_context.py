"""A restored working directory must not claim unrelated explicit input files."""
import json
from pathlib import Path

import pytest

from ashare_daily.operations import daily
from ashare_daily.research.evidence import load_local_materials
from ashare_daily.screening.snapshots import read_bundle
from test_archive_paths import archive_fixture
from test_m4_daily import project, execute, FakeServices
from test_m2_engine import input_snapshot
from test_m21_engine import m21_input
from test_m3_workflow import workflow, material, generate, fake_client, output

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def restored_working_directory(tmp_path, monkeypatch):
    root = archive_fixture(tmp_path / "unrelated-restored-owner", {
        "outputs/research/m3/marker.raw": b"OFFLINE archived owner"})
    monkeypatch.chdir(root)
    # Models a module imported from a restored checkout, on any host OS.
    monkeypatch.setitem(daily.valid_report.__kwdefaults__, "anchor", root)
    return root


def test_explicit_corrupt_bundle_reaches_its_content_hash_check(tmp_path, restored_working_directory):
    path = tmp_path / "explicit-input.json"
    path.write_text(json.dumps({"schema_version": "m2-adjusted-bundle-v1", "manifest_hash": "0" * 64}), encoding="utf-8")
    with pytest.raises(ValueError, match="调整数据包哈希不匹配"):
        read_bundle(path)


def test_explicit_manual_import_is_not_a_foreign_archive_reference(tmp_path, restored_working_directory):
    path = tmp_path / "explicit-manual.json"
    path.write_text(json.dumps({"schema_version": "m3-local-materials-v1", "sources": [], "evidence": []}), encoding="utf-8")
    result = load_local_materials(path)
    assert result["acquisition_mode"] == "manual"
    assert result["import_file"] == str(path)


def test_m3_external_explicit_snapshots_keep_full_evidence_checks(workflow, tmp_path, restored_working_directory):
    result = generate(workflow, tmp_path, client=fake_client(output(workflow[3])), config_path=ROOT / "config/m3.json")
    assert result["verification_kind"] == "offline_test"
    assert result["accepted_claim_count"] == 1
    assert result["generation_status"] == "ok"


def test_daily_publication_uses_the_callers_output_root(project, restored_working_directory):
    services = FakeServices()
    result = execute(project, services)
    assert result["generation_status"] == "ok"
    assert Path(result["report"]["json"]).is_relative_to(project)
    repeated = execute(project, FakeServices(fail_stage="calendar"))
    assert repeated["status"] == "reused"
    assert repeated["model_summary"]["call_count"] == 0
