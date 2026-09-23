"""F4-S1 frozen workflow integration; only source readers/material text are doubled."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from ashare_daily import sector_f4s1 as workflow
from ashare_daily.sector_eligibility import collect_sector_eligibility
from ashare_daily.sector_selection import digest
from test_sector_screening import fixture, freeze_selection, seal_stock


ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def setup(tmp_path, monkeypatch, *, count=2, purpose="engineering_validation"):
    selected, original = fixture(count=count, days=120, purpose=purpose)
    config = {"output_directory": "outputs/offline_test/validation"}
    calls = {key: 0 for key in ("history", "benchmark", "gaps", "materials", "index", "technical")}
    strategy = json.loads((ROOT/"config/sector_screening_f4s1.json").read_text(encoding="utf-8"))
    write(tmp_path/"config/sector_screening_f4s1.json", strategy)
    write(tmp_path/"config/m3_sources.json", {"purpose": "offline_test"})
    write(tmp_path/"config/sector_first_daily.json", {"lookback_days": 7, "purpose": "offline_test"})
    source_ref = write(tmp_path/"source/normalized-input-evidence.json", {"mode": "offline_test", "reference": "synthetic version"})
    original["benchmark"]["file_refs"] = [source_ref]
    gap_path = tmp_path/"source/gap-diagnosis.json"
    gap_body = {"definitive_non_trading_dates_by_security": {}, "file_refs": []}
    gap_body["content_hash"] = digest(gap_body)
    write(gap_path, gap_body)
    packet = collect_sector_eligibility(tmp_path, selected, output_directory="source/qualification", cutoff_at="2026-09-11T19:00:00+08:00")
    facts_path = Path(packet.file_ref["path"])
    history = {"calendar": original["calendar"], "securities": original["securities"], "file_refs": [source_ref], "issues": []}
    def choose(root, identity, config_path):
        assert identity == selected["selection_id"]
        return deepcopy(selected), deepcopy(config)
    monkeypatch.setattr(workflow, "_selection", choose)
    def history_reader(*args):
        calls["history"] += 1
        return deepcopy(history)
    def benchmark_reader(*args):
        calls["benchmark"] += 1
        # Keep source evidence in the common verified reference list; this is a
        # source-reader fixture, while the actual numerical core runs below.
        return deepcopy(original["benchmark"])
    def gap_reader(root, path, *args):
        calls["gaps"] += 1
        return json.loads(Path(path).read_text(encoding="utf-8"))
    def index_reader(*args):
        calls["index"] += 1
        return {"content_hash": "c"*64}, {}, []
    def materials(root, selection, technical, eligibility, *, cutoff_at):
        calls["materials"] += 1
        assert technical["selection_id"] == eligibility["selection_id"] == selection["selection_id"]
        assert technical["purpose"] == eligibility["purpose"] == purpose
        return {"schema_version": "f4s1-company-materials-v1", "purpose": purpose, "production_eligible": False,
            "selection_id": selection["selection_id"], "packages": [], "model_calls": 0, "model_tokens": 0}
    monkeypatch.setattr("ashare_daily.sector_history.screening_history_inputs", history_reader)
    monkeypatch.setattr("ashare_daily.sector_benchmark.read_benchmark", benchmark_reader)
    monkeypatch.setattr("ashare_daily.sector_gap_diagnosis.read_gap_diagnosis", gap_reader)
    monkeypatch.setattr("ashare_daily.sector_company_evidence._index", index_reader)
    monkeypatch.setattr("ashare_daily.sector_company_evidence.prepare_company_materials", materials)
    from ashare_daily import sector_screening
    actual_evaluate = sector_screening.evaluate_selection
    def numerical_core(*args):
        calls["technical"] += 1
        return actual_evaluate(*args)
    monkeypatch.setattr(sector_screening, "evaluate_selection", numerical_core)
    return selected, original, gap_path, facts_path, source_ref, calls


def prepare(tmp_path, selected, gap_path, facts_path, **kwargs):
    return workflow.prepare_revision(tmp_path, selected["selection_id"], gap_path=gap_path, facts_path=facts_path, **kwargs)


def hashes(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*") if path.is_file()}


def test_nonempty_same_inputs_are_idempotent_and_share_real_technical_and_qualification_cores(tmp_path, monkeypatch):
    selected, _, gap_path, facts_path, _, calls = setup(tmp_path, monkeypatch)
    first = prepare(tmp_path, selected, gap_path, facts_path)
    directory = Path(first["output_directory"])
    frozen = hashes(directory)
    second = prepare(tmp_path, selected, gap_path, facts_path)
    assert first["revision_id"] == second["revision_id"] and second["reused_revision"]
    assert frozen == hashes(directory)
    assert calls["technical"] == 1 and calls["materials"] == 1
    assert first["counts"]["stock_count"] == 2 and first["counts"]["technical_pass"] == 2
    assert first["counts"]["eligibility_pending"] == 2
    assert first["counts"]["formal_candidates"] == first["counts"]["model_queue"] == 0
    assert not (tmp_path/"data").exists()


def test_nonempty_interruption_then_resume_uses_frozen_inputs_without_refetch(tmp_path, monkeypatch):
    selected, _, gap_path, facts_path, _, calls = setup(tmp_path, monkeypatch)
    with pytest.raises(InterruptedError, match="technical"):
        prepare(tmp_path, selected, gap_path, facts_path, interrupt_after="technical")
    directories = list((tmp_path/"outputs/offline_test/validation"/selected["selection_id"]/"f4s1/revisions").iterdir())
    assert len(directories) == 1
    directory = directories[0]
    assert (directory/"technical_evaluation.json").is_file() and not (directory/"manifest.json").exists()
    frozen_technical = (directory/"technical_evaluation.json").read_bytes()
    counts_before = deepcopy(calls)
    def forbidden(*args):
        pytest.fail("resume refetched history or benchmark instead of frozen inputs")
    monkeypatch.setattr("ashare_daily.sector_history.screening_history_inputs", forbidden)
    monkeypatch.setattr("ashare_daily.sector_benchmark.read_benchmark", forbidden)
    resumed = prepare(tmp_path, selected, gap_path, facts_path, revision=directory.name)
    assert resumed["revision_id"] == directory.name and resumed["counts"]["stock_count"] == 2
    assert calls["technical"] == counts_before["technical"]+1
    assert (directory/"technical_evaluation.json").read_bytes() == frozen_technical
    assert workflow.read_revision(directory, selected, root=tmp_path)["counts"]["stock_count"] == 2


def test_zero_production_never_reads_history_qualification_company_or_gap_sources(tmp_path, monkeypatch):
    selected, _, _, _, _, _ = setup(tmp_path, monkeypatch, count=0, purpose="production")
    def forbidden(*args, **kwargs):
        pytest.fail("S=0 triggered source or company work")
    for target in ("sector_history.screening_history_inputs", "sector_benchmark.read_benchmark",
                   "sector_gap_diagnosis.read_gap_diagnosis", "sector_eligibility.read_field_facts",
                   "sector_company_evidence.prepare_company_materials", "sector_company_evidence._index"):
        monkeypatch.setattr("ashare_daily."+target, forbidden)
    result = prepare(tmp_path, selected, Path("must-not-read-gaps"), Path("must-not-read-facts"))
    assert result["status"] == "not_applicable" and result["counts"]["stock_count"] == 0
    assert result["counts"]["formal_candidates"] == result["counts"]["local_material_queue"] == 0
    assert result["purpose"] == "production" and result["production_eligible"] is False
    assert result["network_requests"] == result["database_writes"] == result["model_calls"] == 0


def test_nonempty_production_is_not_enabled_even_by_dry_run(tmp_path, monkeypatch):
    selected, _, gap_path, facts_path, _, calls = setup(tmp_path, monkeypatch, purpose="production")
    for dry in (True, False):
        with pytest.raises(ValueError, match="nonempty_production"):
            prepare(tmp_path, selected, gap_path, facts_path, dry_run=dry)
    assert not any(calls.values())


def test_engineering_output_never_uses_production_namespace(tmp_path, monkeypatch):
    selected, _, gap_path, facts_path, _, _ = setup(tmp_path, monkeypatch)
    result = prepare(tmp_path, selected, gap_path, facts_path)
    assert "outputs/offline_test/validation" in Path(result["output_directory"]).as_posix()
    assert not (tmp_path/"outputs/research").exists()
    report = workflow.read_revision(result["output_directory"], selected, root=tmp_path)
    assert report["purpose"] == "engineering_validation" and report["production_eligible"] is False
    assert report["f4s2_ready"] is False


@pytest.mark.parametrize("filename", ["technical_evaluation.json", "eligibility_evidence.csv", "candidate_evidence/materials.json"])
def test_manifest_detects_modified_immutable_artifact(tmp_path, monkeypatch, filename):
    selected, _, gap_path, facts_path, _, _ = setup(tmp_path, monkeypatch)
    result = prepare(tmp_path, selected, gap_path, facts_path)
    directory = Path(result["output_directory"])
    with (directory/filename).open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="hash"):
        workflow.read_revision(directory, selected, root=tmp_path)


def test_manifest_cannot_omit_a_required_artifact_after_rehash(tmp_path, monkeypatch):
    selected, _, gap_path, facts_path, _, _ = setup(tmp_path, monkeypatch)
    result = prepare(tmp_path, selected, gap_path, facts_path)
    directory = Path(result["output_directory"])
    manifest = json.loads((directory/"manifest.json").read_text())
    manifest["files"].pop("screening_inputs.json")
    manifest["content_hash"] = digest({key: value for key, value in manifest.items() if key != "content_hash"})
    write(directory/"manifest.json", manifest)
    with pytest.raises(ValueError, match="incomplete"):
        workflow.read_revision(directory, selected, root=tmp_path)


@pytest.mark.parametrize("resume_complete", [True, False])
def test_source_ref_tamper_blocks_completed_report_and_interrupted_resume(tmp_path, monkeypatch, resume_complete):
    selected, _, gap_path, facts_path, source_ref, _ = setup(tmp_path, monkeypatch)
    if resume_complete:
        result = prepare(tmp_path, selected, gap_path, facts_path)
        directory = Path(result["output_directory"])
    else:
        with pytest.raises(InterruptedError):
            prepare(tmp_path, selected, gap_path, facts_path, interrupt_after="technical")
        directory = next((tmp_path/"outputs/offline_test/validation"/selected["selection_id"]/"f4s1/revisions").iterdir())
    Path(source_ref["path"]).write_text("tampered source evidence")
    with pytest.raises(ValueError, match="source|hash"):
        if resume_complete:
            workflow.read_revision(directory, selected, root=tmp_path)
        else:
            prepare(tmp_path, selected, gap_path, facts_path, revision=directory.name)


@pytest.mark.parametrize("field,value", [("mode", "research"), ("target_date", "2026-01-01"), ("purpose", "production")])
def test_report_semantic_scope_mismatch_rejected_even_with_updated_hashes(tmp_path, monkeypatch, field, value):
    selected, _, gap_path, facts_path, _, _ = setup(tmp_path, monkeypatch)
    result = prepare(tmp_path, selected, gap_path, facts_path)
    directory = Path(result["output_directory"])
    report = json.loads((directory/"technical_eligibility_report.json").read_text(encoding="utf-8"))
    report[field] = value
    report["content_hash"] = digest({key: item for key, item in report.items() if key != "content_hash"})
    report_ref = write(directory/"technical_eligibility_report.json", report)
    manifest = json.loads((directory/"manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["technical_eligibility_report.json"] = report_ref["sha256"]
    manifest["content_hash"] = digest({key: item for key, item in manifest.items() if key != "content_hash"})
    write(directory/"manifest.json", manifest)
    with pytest.raises(ValueError, match="scope|binding"):
        workflow.read_revision(directory, selected, root=tmp_path)


def test_new_source_version_creates_revision_without_overwriting_old(tmp_path, monkeypatch):
    selected, _, gap_path, facts_path, _, _ = setup(tmp_path, monkeypatch)
    first = prepare(tmp_path, selected, gap_path, facts_path)
    old_directory = Path(first["output_directory"])
    old_hashes = hashes(old_directory)
    replacement = tmp_path/"source/new-gap-version.json"
    new_gap = {"definitive_non_trading_dates_by_security": {}, "source_revision": "new-offline-observation", "file_refs": []}
    new_gap["content_hash"] = digest(new_gap)
    write(replacement, new_gap)
    second = prepare(tmp_path, selected, replacement, facts_path)
    assert second["revision_id"] != first["revision_id"]
    assert hashes(old_directory) == old_hashes


@pytest.mark.parametrize("revision", ["../other", "f4s1-not-a-hash", "f3s-"+"a"*24])
def test_revision_path_cannot_escape_or_select_wrong_stage(tmp_path, monkeypatch, revision):
    selected, _, gap_path, facts_path, _, _ = setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="invalid_revision"):
        prepare(tmp_path, selected, gap_path, facts_path, revision=revision)


def test_dry_run_does_not_create_revision_or_read_data_sources(tmp_path, monkeypatch):
    selected, _, gap_path, facts_path, _, calls = setup(tmp_path, monkeypatch)
    before = hashes(tmp_path)
    result = prepare(tmp_path, selected, gap_path, facts_path, dry_run=True)
    assert result["status"] == "dry_run" and not any(calls.values())
    assert hashes(tmp_path) == before


def test_target_halt_remains_in_technical_denominator_as_not_applicable(tmp_path, monkeypatch):
    selected, original, gap_path, _, _, _ = setup(tmp_path, monkeypatch, count=1)
    selected["members"][0]["statuses"]["suspended"] = {"value": True, "effective_date": selected["target_date"],
        "evidence_id": "OFFLINE_TEST_full_day_halt", "observed_at": selected["cutoff_at"]}
    stock = original["securities"][selected["members"][0]["security_id"]]
    for rows in (stock["raw_records"], stock["adjustment_window"]["records"]):
        rows[-1].update(tradestatus=False, open=None, high=None, low=None, close=None, amount_cny="0", volume_shares=0)
    seal_stock(stock)
    freeze_selection(selected)
    facts = collect_sector_eligibility(tmp_path, selected, output_directory="source/halt-qualification", cutoff_at="2026-09-11T19:00:00+08:00")
    result = prepare(tmp_path, selected, gap_path, facts.file_ref["path"])
    assert result["counts"]["technical_not_applicable"] == 1
    assert sum(result["counts"]["technical_"+state] for state in ("pass", "fail", "unknown", "not_applicable")) == 1


def test_material_original_body_tamper_blocks_frozen_report(tmp_path, monkeypatch):
    selected, _, gap_path, facts_path, _, _ = setup(tmp_path, monkeypatch, count=1)
    raw_ref = write(tmp_path/"source/company-original-body.json", {"mode": "offline_test", "content": "OFFLINE_TEST registered body"})
    collection_ref = write(tmp_path/"source/company-collection.json", {"mode": "offline_test"})
    registry_ref = write(tmp_path/"source/company-registry.json", {"mode": "offline_test"})
    def material_fixture(root, selected, technical, eligibility, *, cutoff_at):
        return {"schema_version": "f4s1-company-materials-v1", "purpose": "engineering_validation", "production_eligible": False,
            "selection_id": selected["selection_id"], "packages": [{"documents": [{"provenance": {
                "raw_source": raw_ref, "collection": collection_ref, "registry_snapshot": registry_ref}}]}],
            "model_calls": 0, "model_tokens": 0}
    monkeypatch.setattr("ashare_daily.sector_company_evidence.prepare_company_materials", material_fixture)
    result = prepare(tmp_path, selected, gap_path, facts_path)
    Path(raw_ref["path"]).write_text("changed original body")
    with pytest.raises(ValueError, match="source|hash"):
        workflow.read_revision(result["output_directory"], selected, root=tmp_path)
