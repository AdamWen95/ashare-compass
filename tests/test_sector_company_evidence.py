"""Bounded offline contracts; fixture articles are not online source acceptance."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import sqlite3

import pytest

from ashare_daily.research.evidence import EvidenceStore, build_evidence, validate_claims
from ashare_daily.sector_company_evidence import prepare_company_materials
from ashare_daily.sector_selection import digest

SEEN = "2026-09-11T20:00:00+08:00"
CUTOFF = "2026-09-11T21:00:00+08:00"
BODY = "离线夹具公司（600001）公告：公司的主营业务为设备检测服务。本次合作尚未生效，前期合同已经终止，未来履约仍存在风险。此段仅供离线合同核验。"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def inputs(count=1, technical=None, eligibility=None, purpose="engineering_validation"):
    members = [{"security_id": "offline-security-"+str(i), "exchange": "SSE", "code": str(600001+i),
        "name": "离线夹具公司" if not i else "另一个离线主体", "board": "sse_main", "sector_ids": ["not-business-evidence"]} for i in range(count)]
    selection = {"schema_version": "f3s-validation-selection-v1", "mode": "offline_test", "purpose": purpose,
        "production_eligible": False, "automatic_selection": False, "source_selection_id": "sector-offline-fixture",
        "market_scope": "sse_szse_a", "research_mode": "sector_first", "target_date": "2026-09-11", "cutoff_at": SEEN,
        "members": members, "selected_security_count": count}
    signature = digest(selection)
    selection.update(content_hash=signature, selection_id=("validation-sector-" if purpose == "engineering_validation" else "sector-")+"2026-09-11-"+signature[:20])
    report = {"schema_version": "f3s-screening-result-v1", "selection_id": selection["selection_id"], "target_date": "2026-09-11",
        "mode": "offline_test", "purpose": purpose, "production_eligible": False, "cutoff_at": SEEN,
        "evaluations": [{"security_id": member["security_id"], "symbol": "sh."+member["code"],
            "technical_status": (technical or ["pass"]*count)[i], "metrics": {"return_21": "0.01"}} for i, member in enumerate(members)]}
    report["result_hash"] = digest(report)
    qualified = {"schema_version": "f4s1-eligibility-result-v1", "selection_id": selection["selection_id"],
        "selection_content_hash": selection["content_hash"], "technical_result_hash": report["result_hash"],
        "target_date": "2026-09-11", "mode": "offline_test", "purpose": purpose, "production_eligible": False, "cutoff_at": SEEN,
        "evaluations": [{**row, "eligibility_status": (eligibility or ["pass"]*count)[i]} for i, row in enumerate(report["evaluations"])]}
    qualified["content_hash"] = digest(qualified)
    return selection, report, qualified


def fixture(root, *, body=BODY, associate=True, fulltext=True, enabled=True, model=False, published="2026-09-11T16:00+08:00"):
    registration = {"source_id": "offline-company-source", "name": "离线来源", "category": "announcement",
        "source_url": "https://example.invalid/", "access_method": "offline_fixture", "usage_limits": "OFFLINE_TEST only",
        "enabled": enabled, "cache_allowed": True, "model_use_allowed": model, "publish_excerpt_allowed": False,
        "content_access": ["metadata_only", "abstract", "fulltext"], "permission_basis": "OFFLINE_TEST local fixture permission only", "checked_at": SEEN}
    article = root/"outputs/offline_test/company_materials/responses/article.raw"
    article.parent.mkdir(parents=True)
    title = "离线夹具公司公告"
    article.write_text('<h1 class="art-title">'+title+'</h1><div class="at-left">2026-09-11 16:00</div><div class="art-con">'+body+'</div>', encoding="utf-8")
    source = {"registration": registration, "adapter": "registered_html", "pages": [{"url": "https://example.invalid/article", "content_type": "fulltext"}]}
    registry = {"schema_version": "m3-source-registry-v1", "sources": [source]}
    write(root/"config/m3_sources.json", registry)
    write(article.parent.parent/"registry_snapshot.json", registry)
    write(article.parent.parent/"result.json", {"requests": [{"url": "https://example.invalid/article", "http_status": 200,
        "status": "ok", "raw_sha256": hashlib.sha256(article.read_bytes()).hexdigest()}]})
    store = EvidenceStore(root/"data/offline_test/company_materials/market.sqlite3", "offline_test")
    store.register_sources([registration])
    item = build_evidence(source_id=registration["source_id"], category="announcement", original_url="https://example.invalid/article",
        raw_locator=str(article), title=title, published_at=published, publication_precision="minute" if published else "unknown",
        first_seen_at=SEEN, fetched_at=SEEN, content_type="fulltext" if fulltext else "metadata_only", content=body if fulltext else "",
        original_publisher="离线来源", acquisition_mode="offline_test",
        security_associations=[{"symbol": "sh.600001", "name": "离线夹具公司", "association_type": "explicit_subject", "basis_quote": body}] if associate and fulltext else [])
    store.ingest([item])
    return store, item, article


def prepare(root, values=None, **kwargs):
    return prepare_company_materials(root, *(values or inputs()), cutoff_at=CUTOFF, **kwargs)


def test_no_index_still_keeps_full_dynamic_denominator_and_never_creates_database(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network forbidden"))
    values = inputs(4, technical=["pass", "pass", "pass", "fail"], eligibility=["pass", "pending", "fail", "pass"])
    result = prepare(tmp_path, values, output_dir=tmp_path/"unused")
    assert result["counts"] == {"stock_count": 4, "material_queue_count": 2, "blocked_count": 2,
        "partial_diagnostic_count": 0, "formal_candidate_count": 0, "model_queue_count": 0}
    assert result["packages"][1]["material_queue_status"] == "diagnostic_pending"
    assert not list(tmp_path.iterdir())
    assert not result["model_queue"] and result["model_calls"] == result["model_tokens"] == result["network_requests"] == 0


def test_production_zero_members_never_uses_engineering_members(tmp_path):
    result = prepare(tmp_path, inputs(0, purpose="production"))
    assert not result["packages"] and not result["evaluations"]
    assert result["status"] == "no_triggered_objects"


def test_wrong_subject_title_or_industry_cannot_establish_company_body(tmp_path):
    store, item, _ = fixture(tmp_path, body="这是另一家企业的情况说明，主体与待研究证券无关。即使行业名称相似，也不代表上市公司的实际主营业务或项目受益情况。", associate=False)
    before = store.path.read_bytes()
    result = prepare(tmp_path)
    package = result["packages"][0]
    assert package["search_record"]["match_count"] == 1  # title matched, not issuer body
    assert package["status"] == "blocked" and not package["documents"]
    assert package["main_business"] is None and package["benefit_evidence"] is None
    assert package["rejected_documents"][0]["reason"] == "explicit_issuer_body_identity_unverified"
    assert store.path.read_bytes() == before
    assert prepare(tmp_path) == result  # stable replay, no fabricated new observation


def test_verified_body_keeps_termination_as_unreviewed_fact_not_benefit_or_model_queue(tmp_path):
    store, item, _ = fixture(tmp_path)
    before = store.path.read_bytes()
    result = prepare(tmp_path)
    package = result["packages"][0]
    assert package["status"] == "partial_diagnostic"
    assert len(package["documents"]) == 1
    assert package["documents"][0]["provenance"]["body_sha256"] == item["content_hash"]
    assert package["main_business"] is None and package["benefit_evidence"] is None
    assert not package["facts"] and not package["inferences"] and not package["opinions"]
    assert not package["production_eligible"] and not package["model_queue_eligible"]
    assert package["documents"][0]["model_use_allowed"] is False
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("field", ["raw", "index_hash", "source_hash"])
def test_tampering_never_becomes_verified_material(tmp_path, field):
    store, item, article = fixture(tmp_path)
    if field == "raw":
        article.write_bytes(article.read_bytes()+b" altered")
    else:
        with sqlite3.connect(store.path) as connection:
            if field == "index_hash":
                payload = deepcopy(item)
                payload["content"] += "altered"
                connection.execute("UPDATE m3_evidence SET payload_json=?", (json.dumps(payload),))
            else:
                connection.execute("UPDATE m3_source_registry SET version_hash=?", ("0"*64,))
    if field == "source_hash":
        with pytest.raises(ValueError, match="registration hash"):
            prepare(tmp_path)
    else:
        result = prepare(tmp_path)
        assert result["packages"][0]["status"] == "blocked"
        assert not result["packages"][0]["documents"]
        if field == "index_hash":
            assert result["index_rejections"] and result["index_snapshot"]["evidence_count"] == 1


@pytest.mark.parametrize("options", [{"fulltext": False}, {"enabled": False}, {"published": None},
    {"published": "2026-09-12T16:00+08:00"}, {"associate": False}])
def test_unknown_date_permission_metadata_or_subject_remain_blocked(tmp_path, options):
    fixture(tmp_path, **options)
    result = prepare(tmp_path)
    assert result["packages"][0]["status"] == "blocked"
    assert result["network_requests"] == 0


def test_existing_evidence_id_does_not_validate_unsupported_claim(tmp_path):
    _, item, _ = fixture(tmp_path)
    claim = {"claim_id": "unsupported", "claim_type": "fact", "text": "该公司已获得确定受益。", "symbol": "sh.600001",
        "citations": [{"evidence_id": item["evidence_id"], "quote": item["content"], "locator": item["raw_locator"]}],
        "metric_ids": [], "risks": [], "unknowns": []}
    result = validate_claims({"claims": [claim]}, [item], {"sh.600001": {"name": "离线夹具公司"}}, {}, CUTOFF)
    assert not result["accepted_claims"] and result["rejected_claims"]


@pytest.mark.parametrize("mutation", ["date", "technical_hash", "mode", "duplicates", "production_shell"])
def test_input_bindings_fail_closed(tmp_path, mutation):
    selected, technical, qualified = inputs()
    if mutation == "date":
        qualified["target_date"] = "2026-09-10"
    elif mutation == "technical_hash":
        qualified["technical_result_hash"] = "0"*64
    elif mutation == "mode":
        qualified["mode"] = "research"
    elif mutation == "duplicates":
        qualified["evaluations"] *= 2
    else:
        qualified["production_eligible"] = True
    qualified["content_hash"] = digest({key: value for key, value in qualified.items() if key != "content_hash"})
    with pytest.raises(ValueError):
        prepare(tmp_path, (selected, technical, qualified))


def test_mode_marker_cannot_read_real_index_as_fixture(tmp_path):
    store, _, _ = fixture(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE m3_metadata SET value='real' WHERE key='verification_kind'")
    with pytest.raises(ValueError, match="provenance mode"):
        prepare(tmp_path)


def test_new_evidence_index_version_changes_material_fingerprint_without_mutating_old_item(tmp_path):
    store, item, _ = fixture(tmp_path)
    first = prepare(tmp_path)
    changed = {key: value for key, value in item.items() if key not in {"evidence_id", "content_version", "content_hash"}}
    changed.update(title="后续离线更正", original_url="https://example.invalid/correction", revision_of=item["evidence_id"])
    store.ingest([build_evidence(**changed)])
    second = prepare(tmp_path)
    assert first["index_snapshot"]["evidence_count"] == 1 and second["index_snapshot"]["evidence_count"] == 2
    assert first["input_fingerprint"] != second["input_fingerprint"]
    with sqlite3.connect(store.path) as connection:
        old = json.loads(connection.execute("SELECT payload_json FROM m3_evidence WHERE evidence_id=?", (item["evidence_id"],)).fetchone()[0])
    assert old == item


def test_later_preparation_keeps_target_event_window_and_marks_late_observation(tmp_path):
    fixture(tmp_path)
    values = inputs()
    result = prepare_company_materials(tmp_path, *values, cutoff_at="2026-09-14T15:00:00+08:00")
    assert result["query_start"] == "2026-09-08T00:00:00+08:00"
    assert result["event_cutoff"] == SEEN
    assert result["cutoff_at"] == "2026-09-14T15:00:00+08:00"
    write(tmp_path/"config/sector_first_daily.json", {"first_query_lookback_days": 4})
    changed = prepare_company_materials(tmp_path, *values, cutoff_at="2026-09-14T15:00:00+08:00")
    assert changed["query_start"] == "2026-09-07T00:00:00+08:00"
    assert changed["input_fingerprint"] != result["input_fingerprint"]


def test_same_body_with_different_event_id_is_not_independent_evidence(tmp_path):
    store, item, _ = fixture(tmp_path)
    duplicate = {key: value for key, value in item.items() if key not in {"evidence_id", "content_version", "content_hash"}}
    duplicate["event_key"] = "another-label-on-same-body"
    store.ingest([build_evidence(**duplicate)])
    package = prepare(tmp_path)["packages"][0]
    assert len(package["documents"]) == 1 and package["search_record"]["match_count"] == 2
    assert any(row["reason"] == "syndicated_duplicate" for row in package["rejected_documents"])


def test_existing_real_m3_body_is_a_wrong_subject_counterexample_only(tmp_path):
    actual = Path(__file__).resolve().parents[1]/"data/research/market.sqlite3"
    if not actual.exists():
        pytest.skip("real archived M3 index is not distributed with source-only checkout")
    with sqlite3.connect(actual.as_uri()+"?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT payload_json FROM m3_evidence ORDER BY evidence_id LIMIT 1").fetchone()
    if row is None:
        pytest.skip("real archive has no local company material to use as wrong-subject counterexample")
    # Transplant only existing text into an explicitly offline fixture; its real
    # subject is not this fixture issuer and this proves no live acquisition.
    body = json.loads(row[0])["content"]
    fixture(tmp_path, body=body, associate=False)
    package = prepare(tmp_path)["packages"][0]
    assert package["status"] == "blocked" and not package["documents"]
    assert package["search_record"]["network_requests"] == 0


def test_later_actual_observation_is_not_backdated_into_preparation_cutoff(tmp_path):
    store, item, _ = fixture(tmp_path)
    late = {**item, "first_seen_at": "2026-09-12T12:00:00+08:00", "fetched_at": "2026-09-12T12:00:00+08:00"}
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE m3_evidence SET first_seen_at=?,payload_json=?", (late["first_seen_at"], json.dumps(late)))
    package = prepare(tmp_path)["packages"][0]
    assert package["status"] == "blocked"
    assert package["rejected_documents"][0]["reason"] == "observed_after_preparation_cutoff"
