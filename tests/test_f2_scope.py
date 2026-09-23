"""Explicit SSE/SZSE scope contracts. Every fixture remains offline_test."""
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path

import pytest

from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.providers.exchange_universe import discover_exchange_lists, SSE_ENDPOINT, SSE_METADATA_ENDPOINT
from ashare_daily.universe import BOARDS, UniverseStore, scope_boards, sync_universe
from ashare_daily.universe_acceptance import write_acceptance_details
from ashare_daily.universe_service import UniverseConfig, sync_date
from test_f1_universe import inputs, row, rows
from test_f11_exchange_universe import approved, response, sse_body, sse_raw, szse_body, szse_raw

ROOT = Path(__file__).resolve().parents[1]
FOUR = ("sse_main", "szse_main", "chinext", "star")


def scoped_input(*, day="2026-09-11", count=124, unknown_states=False):
    records = [row(i, board=FOUR[i % 4], day=day, **({"statuses": {}} if unknown_states else {})) for i in range(count)]
    values = inputs(records, day=day)
    values["manifests"][0]["coverage_boards"] = list(FOUR)
    return values


def test_scope_names_have_fixed_complete_board_sets():
    assert scope_boards() == BOARDS
    assert scope_boards("sse_szse_a") == FOUR
    with pytest.raises(ValueError):
        scope_boards("sse_only")


def test_observed_5218_count_is_not_a_limit_on_later_dynamic_discovery(tmp_path):
    with UniverseStore(tmp_path / "offline.sqlite3", mode="offline_test") as store:
        first = sync_universe(store, **scoped_input(day="2026-09-10", count=5218), scope="sse_szse_a")
        later = sync_universe(store, **scoped_input(count=5224), scope="sse_szse_a")
        assert first["ordinary_a_count"] == 5218
        assert later["ordinary_a_count"] == 5224
        assert len(later["members"]) == 5224
        assert sum(later["board_counts"].values()) == 5224
        assert later["structural_verified"]
        assert later["universe_verified"] is False  # Still an offline fixture.


def test_explicit_four_board_scope_passes_contract_but_same_input_all_a_blocks(tmp_path):
    with UniverseStore(tmp_path/"offline.sqlite3", mode="offline_test") as store:
        four = sync_universe(store, **scoped_input(), scope="sse_szse_a")
        assert four["scope"] == "sse_szse_a"
        assert four["structural_verified"]
        assert four["board_counts"] == dict.fromkeys(FOUR, 31)
        assert four["excluded_boards"] == ["bse"]
        assert not four["universe_verified"]  # Provenance never becomes online.
        assert not four["collection_ready"]
        assert not four["research_ready"]
        all_a = sync_universe(store, **scoped_input())
        assert "board_empty:bse" in all_a["blockers"]
        assert "board_source_unverified:bse" in all_a["blockers"]


def test_scope_transition_preserves_ids_without_false_bse_delisting(tmp_path):
    with UniverseStore(tmp_path/"offline.sqlite3", mode="offline_test") as store:
        before = sync_universe(store, **inputs(rows(25), day="2026-09-10"))
        old_hash = before["content_hash"]
        retained = [deepcopy(r) for r in rows(25, day="2026-09-11") if r["board"] != "bse"]
        data = inputs(retained, day="2026-09-11")
        data["manifests"][0]["coverage_boards"] = list(FOUR)
        after = sync_universe(store, **data, scope="sse_szse_a")
        assert after["structural_verified"]
        assert after["changes"]["previous_snapshot_id"] is None
        assert after["changes"]["coverage_baseline_snapshot_id"] is None
        assert after["changes"]["missing_unexplained"] == []
        assert after["changes"]["delisted_with_evidence"] == []
        assert after["changes"]["change_kind"] == "scope_change"
        assert after["changes"]["scope_change"]["from_scope"] == "all_a"
        assert after["changes"]["scope_change"]["excluded_boards"] == ["bse"]
        ids = {r["code"]: r["security_id"] for r in before["members"]}
        assert all(ids[r["code"]] == r["security_id"] for r in after["members"])
        assert store.get_snapshot(before["snapshot_id"])["content_hash"] == old_hash
        assert sync_universe(store, **data, scope="sse_szse_a") == after


def test_same_day_scope_change_is_recorded_without_reusing_failed_conclusion(tmp_path):
    with UniverseStore(tmp_path/"offline.sqlite3", mode="offline_test") as store:
        before = sync_universe(store, **scoped_input())  # BSE failure in prior all_a snapshot.
        data = scoped_input()
        data["observed_at"] = "2026-09-11T21:06:00+08:00"
        after = sync_universe(store, **data, scope="sse_szse_a")
        assert not before["structural_verified"] and after["structural_verified"]
        assert after["changes"]["scope_change"]["previous_snapshot_id"] == before["snapshot_id"]
        assert after["content_hash"] != before["content_hash"]


def test_snapshot_queries_and_drop_baselines_are_isolated_by_scope(tmp_path):
    with UniverseStore(tmp_path/"offline.sqlite3", mode="offline_test") as store:
        all_a = sync_universe(store, **inputs(rows(10, day="2026-09-09"), day="2026-09-09"))
        four = sync_universe(store, **scoped_input(day="2026-09-10", count=8), scope="sse_szse_a")
        assert store.latest_snapshot(scope="all_a")["snapshot_id"] == all_a["snapshot_id"]
        assert store.latest_snapshot(scope="sse_szse_a")["snapshot_id"] == four["snapshot_id"]
        assert store.latest_verified_snapshot("2026-09-11", scope="all_a")["snapshot_id"] == all_a["snapshot_id"]
        dropped = scoped_input(count=7)
        result = sync_universe(store, **dropped, scope="sse_szse_a")
        assert result["changes"]["coverage_baseline_snapshot_id"] == four["snapshot_id"]
        assert len(result["changes"]["missing_unexplained"]) == 1
        assert "board_count_drop_unexplained:star" in result["blockers"]
        assert not any(reason.endswith(":bse") for reason in result["blockers"])


def test_known_out_of_scope_member_is_retained_and_separately_classified(tmp_path):
    with UniverseStore(tmp_path/"offline.sqlite3", mode="offline_test") as store:
        result = sync_universe(store, **inputs(rows(10)), scope="sse_szse_a")
        assert result["discovered_unique"] == 10
        assert result["ordinary_a_count"] == 8
        outside = [r for r in result["members"] if r["board"] == "bse"]
        assert len(outside) == 2
        assert all(r["discovery_classification"] == "outside_requested_scope" for r in outside)
        assert all(not r["collection_ready"] and not r["research_ready"] for r in outside)


def test_unknown_risk_states_do_not_invent_research_readiness(tmp_path):
    with UniverseStore(tmp_path/"offline.sqlite3", mode="offline_test") as store:
        snapshot = sync_universe(store, **scoped_input(unknown_states=True), scope="sse_szse_a")
        assert snapshot["structural_verified"]
        assert snapshot["unknown_status_count"] == 124
        assert snapshot["status_verified"] is False
        assert snapshot["research_ready"] is False
        assert "risk_status_unknown" in snapshot["research_readiness_reasons"]
        assert all(r["research_ready"] is False for r in snapshot["members"])


@pytest.mark.parametrize("missing", FOUR)
def test_four_scope_cannot_remove_any_required_board(missing):
    config = json.loads((ROOT/"config/universe_f11.json").read_text("utf-8"))
    config.update(scope="sse_szse_a", required_boards=[b for b in FOUR if b != missing])
    with pytest.raises(ValueError, match="四类板块"):
        UniverseConfig.model_validate(config)


def test_four_scope_accepts_two_site_registry_but_all_a_requires_three():
    config = json.loads((ROOT/"config/universe_f11.json").read_text("utf-8"))
    config.update(scope="sse_szse_a", required_boards=list(FOUR))
    config["sources"][1]["sites"] = config["sources"][1]["sites"][:2]
    assert UniverseConfig.model_validate(config).scope == "sse_szse_a"
    config.update(scope="all_a", required_boards=list(BOARDS))
    with pytest.raises(ValueError, match="全部三站"):
        UniverseConfig.model_validate(config)


def fake_exchange_transport(endpoint, params, **kwargs):
    if endpoint == SSE_METADATA_ENDPOINT:
        return response({"result": [{"COMPANY_CODE": "900002", "A_STOCK_CODE": "900002", "SEC_TYPE": "科创A"}]})
    if endpoint == SSE_ENDPOINT:
        kind = params["STOCK_TYPE"]
        return response(sse_body([sse_raw("900001" if kind == "1" else "900002", kind)]))
    return response(szse_body([szse_raw("890001"), szse_raw("890002", "创业板")]))


def test_exchange_adapter_skips_bse_only_for_explicit_scope_and_keeps_null_business_date(tmp_path):
    pages, manifests, calls = discover_exchange_lists(target=date(2026,9,11), directory=tmp_path/"four",
        permissions=approved("SSE", "SZSE"), mode="offline_test", transport=fake_exchange_transport, scope="sse_szse_a")
    assert len(calls) == 4
    assert {m["provider"] for m in manifests} == {"sse", "szse"}
    assert all(m["source_business_date"] is None and m["temporal_basis"] == "current_snapshot_as_observed"
               for m in manifests if m["provider"] == "sse")
    assert next(m for m in manifests if m["provider"] == "szse")["source_business_date"] == "2026-09-11"
    _, all_manifests, _ = discover_exchange_lists(target=date(2026,9,11), directory=tmp_path/"all",
        permissions=approved("SSE", "SZSE"), mode="offline_test", transport=fake_exchange_transport)
    assert next(m for m in all_manifests if m["provider"] == "bse")["errors"]


def test_existing_service_propagates_scope_without_making_test_collection_ready(tmp_path):
    config = json.loads((ROOT/"config/universe_f11.json").read_text("utf-8"))
    config.update(scope="sse_szse_a", required_boards=list(FOUR), database="offline/universe.sqlite3",
        output_directory="offline/runs", calendar_cache="offline/calendar", exchange_metadata_cache=None,
        config_version="sse-szse-universe-v1")
    (tmp_path/"config.json").write_text(json.dumps(config), encoding="utf-8")
    result = sync_date(project=tmp_path, config_path="config.json", target=date(2026,9,11),
        now=datetime(2026,9,11,23,tzinfo=SHANGHAI), client=object(),
        calendar_resolver=lambda **kwargs: {"status":"verified", "calendar_verified":True, "resolved_trade_date":"2026-09-11"},
        exchange_transport=fake_exchange_transport, offline_test=True)
    assert result["scope"] == "sse_szse_a"
    assert result["config_version"] == "sse-szse-universe-v1"
    frozen = json.loads(Path(result["snapshot_path"]).read_text("utf-8"))
    assert frozen["config_version"] == "sse-szse-universe-v1"
    assert frozen["snapshot_id"] == result["snapshot_id"]
    assert result["structural_verified"]
    assert set(result["board_coverage"]) == set(FOUR)
    assert set(result["board_acceptance"]) == set(FOUR)
    assert result["universe_verified"] is result["collection_ready"] is result["research_ready"] is False
    details = json.loads(Path(result["acceptance_details_path"]).read_text("utf-8"))
    assert details["scope"] == "sse_szse_a"
    assert details["excluded_boards"] == ["bse"]


def test_optional_config_version_is_frozen_and_old_payload_is_not_rewritten(tmp_path):
    with UniverseStore(tmp_path/"offline.sqlite3", mode="offline_test") as store:
        legacy = sync_universe(store, **scoped_input(), scope="sse_szse_a")
        original = store.connection.execute("SELECT payload FROM f1_universe_snapshots WHERE snapshot_id=?",
                                            (legacy["snapshot_id"],)).fetchone()[0]
        first = sync_universe(store, **scoped_input(), scope="sse_szse_a", config_version="OFFLINE-scope-v1")
        second = sync_universe(store, **scoped_input(), scope="sse_szse_a", config_version="OFFLINE-scope-v2")
        assert "config_version" not in legacy
        assert len({legacy["snapshot_id"], first["snapshot_id"], second["snapshot_id"]}) == 3
        assert store.get_snapshot(legacy["snapshot_id"]) == legacy
        assert store.get_snapshot(first["snapshot_id"])["config_version"] == "OFFLINE-scope-v1"
        assert store.get_snapshot(second["snapshot_id"])["config_version"] == "OFFLINE-scope-v2"
        assert original == store.connection.execute("SELECT payload FROM f1_universe_snapshots WHERE snapshot_id=?",
                                                   (legacy["snapshot_id"],)).fetchone()[0]
        assert {r["security_id"] for r in legacy["members"]} == {r["security_id"] for r in second["members"]}


@pytest.mark.parametrize("value", ["", "  ", 1, False])
def test_supplied_config_version_must_be_explicit_nonempty_string(tmp_path, value):
    with UniverseStore(tmp_path/"offline.sqlite3", mode="offline_test") as store:
        with pytest.raises(ValueError, match="config_version"):
            sync_universe(store, **scoped_input(), scope="sse_szse_a", config_version=value)


def test_pre_scope_snapshot_payload_and_hash_remain_readable(tmp_path):
    """Build a pre-change shaped artifact in an isolated database, never edit a real one."""
    from ashare_daily.universe import _json
    with UniverseStore(tmp_path/"offline.sqlite3", mode="offline_test") as store:
        payload = sync_universe(store, **inputs())
        legacy = deepcopy(payload)
        for key in ("snapshot_id", "content_hash", "required_boards", "excluded_boards", "collection_ready",
                    "research_ready", "research_readiness_reasons"):
            legacy.pop(key, None)
        for r in legacy["members"]:
            for key in ("collection_ready", "research_ready", "research_readiness_reasons"):
                r.pop(key, None)
        legacy["changes"].pop("change_kind", None)
        legacy["changes"].pop("scope_change", None)
        digest = hashlib.sha256(_json(legacy).encode()).hexdigest()
        legacy.update(content_hash=digest, snapshot_id="universe-"+legacy["requested_date"]+"-"+digest[:20])
        store.connection.execute("INSERT INTO f1_universe_snapshots VALUES(?,?,?,?,?,?,?,?)", (
            legacy["snapshot_id"], legacy["requested_date"], legacy["resolved_trade_date"], legacy["cutoff_at"],
            legacy["observed_at"], digest, legacy["status"], _json(legacy)))
        store.connection.commit()
        assert store.get_snapshot(legacy["snapshot_id"]) == legacy
        assert store.latest_snapshot(scope="all_a")["content_hash"] == digest
        assert store.connection.execute("SELECT version FROM f1_schema").fetchone()[0] == 1
