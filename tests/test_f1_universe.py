"""F1 synthetic metadata tests. Network disabled by conftest; never research output."""

from __future__ import annotations

from copy import deepcopy
import json
import sqlite3
import time
import tracemalloc

import pytest

from ashare_daily.universe import BOARD_EXCHANGE, BOARDS, RISK_STATES, UniverseStore, sync_universe


DAY = "2026-09-10"
STAMP = DAY + "T21:05:00+08:00"


def state(value=False, *, day=DAY, **updates):
    return {"value": value, "source": "synthetic_exchange", "evidence_id": "fixture-status-" + day,
            "verified": True, "effective_from": day, "effective_to": day,
            "observed_at": day + "T21:02:00+08:00", **updates}


def row(index=0, *, board="sse_main", day=DAY, **updates):
    return {"provider": "fixture", "code": f"synthetic_{index:06}", "name": "合成证券",
            "exchange": BOARD_EXCHANGE[board], "board": board, "security_type": "ordinary_a",
            "listing_status": "listed", "listing_date": "2000-01-01", "delisting_date": None,
            "metadata_verified": True, "metadata_source": "synthetic_exchange",
            "statuses": {key: state(day=day) for key in RISK_STATES},
            "provenance_mode": "offline_test", **updates}


def rows(count=125, *, day=DAY):
    return [row(index, board=BOARDS[index % len(BOARDS)], day=day) for index in range(count)]


def inputs(records=None, *, day=DAY):
    records = records if records is not None else rows(day=day)
    chunks = [records[index:index + 50] for index in range(0, len(records), 50)] or [[]]
    pages = [{"provider": "fixture", "dataset": "ordinary_a_metadata", "page_number": index + 1,
              "records": chunk, "terminal": index == len(chunks) - 1,
              "provenance_mode": "offline_test"} for index, chunk in enumerate(chunks)]
    manifests = [{"provider": "fixture", "dataset": "ordinary_a_metadata", "permission_status": "approved",
                  "as_of_date": day, "observed_at": day + "T21:03:00+08:00", "provenance_mode": "offline_test",
                  "lineage_id": "synthetic_exchange", "authoritative": True,
                  "expected_pages": len(chunks), "expected_records": len(records), "complete": True,
                  "coverage_boards": list(BOARDS), "errors": []}]
    return {"requested_date": day, "resolved_trade_date": day, "cutoff_at": day + "T21:00:00+08:00",
            "observed_at": day + "T21:05:00+08:00", "pages": pages, "manifests": manifests,
            "calendar_verified": True}


@pytest.fixture
def store(tmp_path):
    with UniverseStore(tmp_path / "offline_test" / "universe.sqlite3", mode="offline_test") as result:
        yield result


def test_more_than_100_preserves_all_five_boards_and_discovery(store):
    result = sync_universe(store, **inputs())
    assert result["ordinary_a_count"] == 125
    assert result["discovered_unique"] == 125
    assert result["board_counts"] == dict.fromkeys(BOARDS, 25)
    assert result["structural_verified"] is True
    assert result["status"] == "sample" and result["universe_verified"] is False
    assert result["formal_candidates_blocked"] is True
    assert result["capability_stage"] == "f1_discovery_only"


@pytest.mark.parametrize("mutation,expected", [
    (lambda data: data["pages"].pop(), "pagination_truncated"),
    (lambda data: data["pages"][1].update(page_number=3), "pagination_gap_or_duplicate"),
    (lambda data: data["pages"][-1].update(terminal=False), "pagination_terminal_unverified"),
    (lambda data: data["manifests"][0].update(expected_records=126), "record_count_mismatch"),
    (lambda data: data["manifests"][0].update(complete=False), "source_completeness_unverified"),
    (lambda data: data["manifests"][0].update(authoritative=False), "source_authority_unverified"),
    (lambda data: data["manifests"][0].update(lineage_id=None), "source_lineage_unknown"),
    (lambda data: data["manifests"][0].update(errors=["network_failed"]), "source_failed"),
])
def test_pagination_and_source_failures_cannot_get_complete(store, mutation, expected):
    data = inputs()
    mutation(data)
    result = sync_universe(store, **data)
    assert result["status"] == "blocked"
    assert any(reason.startswith(expected) for reason in result["blockers"])


def test_empty_board_blocks_even_when_other_boards_have_thousands(store):
    records = [item for item in rows(1200) if item["board"] != "bse"]
    result = sync_universe(store, **inputs(records))
    assert result["ordinary_a_count"] == 960
    assert result["board_counts"]["bse"] == 0
    assert "board_empty:bse" in result["blockers"]
    assert result["scope"] == "all_a"


def test_unexplained_board_drop_and_missing_are_not_delisting(store):
    previous = sync_universe(store, **inputs())
    new = rows(day="2026-09-11")[:-1]
    result = sync_universe(store, **inputs(new, day="2026-09-11"))
    assert result["changes"]["previous_snapshot_id"] == previous["snapshot_id"]
    assert len(result["changes"]["missing_unexplained"]) == 1
    assert result["changes"]["delisted_with_evidence"] == []
    assert "board_count_drop_unexplained:bse" in result["blockers"]
    assert result["status"] == "blocked"


def test_verified_delisting_date_explains_count_change_but_retains_security(store):
    original = sync_universe(store, **inputs())
    new = rows(day="2026-09-11")
    new[-1].update(listing_status="delisted", delisting_date="2026-09-11")
    result = sync_universe(store, **inputs(new, day="2026-09-11"))
    assert result["ordinary_a_count"] == 124 and result["discovered_unique"] == 125
    assert len(result["changes"]["delisted_with_evidence"]) == 1
    assert not result["changes"]["missing_unexplained"]
    assert not result["blockers"]
    assert store.get_snapshot(original["snapshot_id"]) == original


def test_previously_evidenced_delisting_does_not_require_inactive_row_forever(store):
    sync_universe(store, **inputs())
    new = rows(day="2026-09-11")
    new[-1].update(listing_status="delisted", delisting_date="2026-09-11")
    retired = sync_universe(store, **inputs(new, day="2026-09-11"))
    latest = sync_universe(store, **inputs(rows(day="2026-09-12")[:-1], day="2026-09-12"))
    assert not latest["blockers"] and not latest["changes"]["missing_unexplained"]
    assert store.get_snapshot(retired["snapshot_id"])["changes"]["delisted_with_evidence"]


def test_unknown_metadata_stays_in_status_denominator(store):
    records = rows(10)
    records[0].update(metadata_verified=False, statuses={})
    result = sync_universe(store, **inputs(records))
    assert result["ordinary_a_count"] == 9 and result["discovered_unique"] == 10
    assert result["status_denominator"] == 10 and result["unknown_status_count"] == 1
    assert result["status_verified"] is False


@pytest.mark.parametrize("share_type", ["b_share", "etf", "cdr", "fund", "bond", "index", "preferred", "h_share", "neeq"])
def test_non_a_discovery_is_evidently_classified_not_mixed_into_a_count(store, share_type):
    records = rows() + [row(999, security_type=share_type)]
    result = sync_universe(store, **inputs(records))
    assert result["ordinary_a_count"] == 125 and result["discovered_unique"] == 126
    assert result["classification_counts"]["outside_ordinary_a"] == 1


def test_codes_and_names_do_not_infer_exchange_board_or_share_type(store):
    records = rows() + [row(999, code="sh.600000", name="不是名称判断的ST测试", metadata_verified=False)]
    result = sync_universe(store, **inputs(records))
    unknown = next(member for member in result["members"] if member["code"] == "sh.600000")
    assert unknown["exchange"] == "UNKNOWN" and unknown["board"] == "unknown"
    assert unknown["security_type"] == "unknown"
    assert unknown["statuses"]["st"]["value"] is False  # Explicit dated evidence, not name parsing.
    assert result["unknown_metadata_count"] == 1 and result["status"] == "blocked"


def test_same_code_different_exchanges_has_distinct_stable_ids(store):
    records = rows() + [row(888, board="sse_main", code="same-code"), row(999, board="szse_main", code="same-code")]
    result = sync_universe(store, **inputs(records))
    ids = {member["security_id"] for member in result["members"] if member["code"] == "same-code"}
    assert len(ids) == 2
    assert not result["blockers"]


def test_duplicate_page_record_blocks_and_preserves_expected_count(store):
    records = rows() + [row(0)]
    result = sync_universe(store, **inputs(records))
    assert result["discovered_records"] == 126 and result["discovered_unique"] == 125
    assert any(reason.startswith("duplicate_source_code") for reason in result["blockers"])


def alias_event(**updates):
    return {"provider": "fixture", "exchange": "BSE", "old_code": "old-bse", "new_code": "new-bse",
            "effective_date": "2026-09-11", "evidence_source": "synthetic_bse_mapping",
            "evidence_id": "synthetic-mapping-v1", "verified": True,
            "observed_at": "2026-09-11T21:02:00+08:00", **updates}


def test_bse_code_change_uses_evidence_retains_stable_id_and_alias_intervals(store):
    old_records = rows()
    old_records[4]["code"] = "old-bse"
    original = sync_universe(store, **inputs(old_records))
    old_id = next(member["security_id"] for member in original["members"] if member["code"] == "old-bse")
    new_records = rows(day="2026-09-11")
    new_records[4]["code"] = "new-bse"
    result = sync_universe(store, **inputs(new_records, day="2026-09-11"), alias_events=[alias_event()])
    assert store.resolve_alias("fixture", "BSE", "old-bse", DAY) == old_id
    assert store.resolve_alias("fixture", "BSE", "old-bse", "2026-09-11") is None
    assert store.resolve_alias("fixture", "BSE", "new-bse", "2026-09-11") == old_id
    assert next(member["security_id"] for member in result["members"] if member["code"] == "new-bse") == old_id
    assert not result["changes"]["added"] and not result["changes"]["missing_unexplained"]
    assert result["changes"]["changed"] == [{"security_id": old_id, "fields": {"code": {"before": "old-bse", "after": "new-bse"}}}]
    assert not result["blockers"]
    assert store.get_snapshot(original["snapshot_id"]) == original


def test_unverified_mapping_does_not_merge_old_and_new_codes(store):
    old_records = rows()
    old_records[4]["code"] = "old-bse"
    original = sync_universe(store, **inputs(old_records))
    old_id = next(member["security_id"] for member in original["members"] if member["code"] == "old-bse")
    new_records = rows(day="2026-09-11")
    new_records[4]["code"] = "new-bse"
    result = sync_universe(store, **inputs(new_records, day="2026-09-11"), alias_events=[alias_event(verified=False)])
    assert next(member["security_id"] for member in result["members"] if member["code"] == "new-bse") != old_id
    assert any(reason.startswith("alias_conflict") for reason in result["blockers"])
    assert old_id in result["changes"]["missing_unexplained"]


def test_conflicting_existing_aliases_block_instead_of_merging_companies(store):
    old_records = rows() + [row(900, board="bse", code="old-bse"), row(901, board="bse", code="new-bse")]
    sync_universe(store, **inputs(old_records))
    data = inputs(rows(day="2026-09-11"), day="2026-09-11")
    result = sync_universe(store, **data, alias_events=[alias_event()])
    assert any("conflicts with existing identities" in reason for reason in result["blockers"])


def test_retired_code_cannot_reopen_an_alias_on_a_later_day(store):
    records = rows()
    records[4]["code"] = "old-bse"
    sync_universe(store, **inputs(records))
    next_records = rows(day="2026-09-11")
    next_records[4]["code"] = "new-bse"
    sync_universe(store, **inputs(next_records, day="2026-09-11"), alias_events=[alias_event()])
    later_records = rows(day="2026-09-12")
    later_records[4]["code"] = "old-bse"
    result = sync_universe(store, **inputs(later_records, day="2026-09-12"))
    assert any("expired alias" in reason for reason in result["blockers"])
    assert store.resolve_alias("fixture", "BSE", "old-bse", "2026-09-12") is None


def test_risks_and_newly_listed_are_discovered_before_eligibility(store):
    records = rows()
    for index, risk in enumerate(RISK_STATES):
        records[index]["statuses"][risk] = state(True)
    records[3]["listing_date"] = DAY
    result = sync_universe(store, **inputs(records))
    assert result["ordinary_a_count"] == 125
    assert result["risk_counts"] == dict.fromkeys(RISK_STATES, 1)
    assert result["newly_listed_count"] == 1
    assert sum(item["research_eligibility"] == "excluded_by_verified_risk" for item in result["members"]) == 3
    new = next(member for member in result["members"] if member["newly_listed"])
    assert new["history_status"] == "not_assessed_f1" and new["research_eligibility"] == "pending_history_f2"


@pytest.mark.parametrize("bad_state,reason", [
    (None, "not_reported"),
    (state(False, verified=False), "unverified_evidence"),
    (state(False, effective_to="2026-09-09"), "outside_effective_interval"),
    (state(False, effective_from="2026-08-01", effective_to=None), "status_freshness_unverified"),
    (state(False, observed_at="2026-09-11T21:00:00+08:00"), "unverified_observation_time"),
    (state(False, derived_from_absence=True), "absence_not_evidence"),
    (state(False, published_at=DAY + "T21:01:00+08:00"), "published_after_cutoff"),
])
def test_unknown_risk_never_becomes_normal(store, bad_state, reason):
    records = rows()
    records[0]["statuses"]["st"] = bad_state
    result = sync_universe(store, **inputs(records))
    member = next(member for member in result["members"] if member["code"] == "synthetic_000000")
    assert member["statuses"]["st"]["value"] is None
    assert member["statuses"]["st"]["unknown_reason"] == reason
    assert member["research_eligibility"] == "pending_metadata_or_status"
    assert result["ordinary_a_count"] == 125 and result["unknown_status_count"] == 1
    assert result["status_verified"] is False


def test_verified_current_complete_risk_list_can_support_negative_state(store):
    records = rows()
    records[0]["statuses"]["st"] = state(False, derived_from_absence=True, complete_list_verified=True, list_as_of_date=DAY)
    result = sync_universe(store, **inputs(records))
    assert result["unknown_status_count"] == 0


@pytest.mark.parametrize("patch,expected", [
    ({"calendar_verified": False}, "calendar_unverified"),
    ({"resolved_trade_date": None}, "calendar_unverified"),
    ({"resolved_trade_date": "2026-09-09"}, "requested_trade_date_mismatch"),
    ({"observed_at": DAY + "T20:00:00+08:00"}, "cutoff_not_reached"),
])
def test_date_resolution_blocks_without_guessing_workdays(store, patch, expected):
    data = inputs()
    data.update(patch)
    result = sync_universe(store, **data)
    assert expected in result["blockers"] and result["status"] == "blocked"


def test_source_date_must_match_target(store):
    data = inputs()
    data["manifests"][0]["as_of_date"] = "2026-09-09"
    result = sync_universe(store, **data)
    assert any(reason.startswith("source_date_mismatch") for reason in result["blockers"])


def test_timezone_cross_day_and_backfill_are_explicit(store):
    data = inputs()
    data.update(cutoff_at="2026-09-11T13:00:00+00:00", observed_at="2026-09-11T13:05:00+00:00")
    result = sync_universe(store, **data)
    assert result["observed_at"] == "2026-09-11T21:05:00+08:00"
    assert result["is_backfill"] and result["historical_reconstruction"]
    assert result["requested_date"] == DAY


@pytest.mark.parametrize("patch", [{"cutoff_at": DAY + "T21:00:00"}, {"observed_at": DAY + "T21:00:00"}, {"cutoff_at": "2026-09-09T21:00:00+08:00"}])
def test_naive_and_pre_target_timestamps_rejected(store, patch):
    data = inputs()
    data.update(patch)
    with pytest.raises(ValueError):
        sync_universe(store, **data)


def test_same_source_republication_is_not_independent_reconciliation(store):
    check = {"permission_status": "approved", "verified": True, "lineage_id": "synthetic_exchange",
             "as_of_date": DAY, "board_counts": dict.fromkeys(BOARDS, 25)}
    result = sync_universe(store, **inputs(), reconciliations=[check])
    assert "reconciliation_not_independent" in result["blockers"]


def test_independent_reconciliation_requires_every_board_count(store):
    check = {"permission_status": "approved", "verified": True, "lineage_id": "synthetic_independent",
             "as_of_date": DAY, "board_counts": dict.fromkeys(BOARDS, 25)}
    result = sync_universe(store, **inputs(), reconciliations=[check])
    assert result["reconciliations"][0]["passed"] is True
    check["board_counts"]["bse"] = 26
    revised = sync_universe(store, **inputs(), reconciliations=[check])
    assert "reconciliation_board_mismatch:bse" in revised["blockers"]
    assert revised["snapshot_id"] != result["snapshot_id"]


def test_idempotent_snapshots_are_immutable_and_revisions_preserve_old_input(store):
    first = sync_universe(store, **inputs())
    same = sync_universe(store, **inputs())
    assert first == same
    assert store.connection.execute("SELECT COUNT(*) FROM f1_universe_snapshots").fetchone()[0] == 1
    revised = rows()
    revised[0]["name"] = "有版本的名称修正"
    second = sync_universe(store, **inputs(revised))
    assert second["content_hash"] != first["content_hash"]
    assert store.get_snapshot(first["snapshot_id"]) == first
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.connection.execute("UPDATE f1_universe_snapshots SET status='complete'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.connection.execute("DELETE FROM f1_universe_snapshots")


def test_additive_schema_preserves_legacy_market_database(tmp_path):
    from ashare_daily.storage.market import MarketStore
    path = tmp_path / "compat.sqlite3"
    legacy = MarketStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO market_metadata VALUES('verification_kind','offline_test')")
        previous = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    with UniverseStore(path, mode="offline_test") as target:
        sync_universe(target, **inputs())
    with sqlite3.connect(path) as connection:
        current = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'f1_%' ORDER BY name").fetchall()
    assert current == previous
    assert legacy.row_count() == 0


def test_shrunken_blocked_snapshot_never_resets_verified_baseline(store):
    first = sync_universe(store, **inputs(rows(10)))
    second = sync_universe(store, **inputs(rows(9, day="2026-09-11"), day="2026-09-11"))
    third = sync_universe(store, **inputs(rows(9, day="2026-09-12"), day="2026-09-12"))
    assert first["structural_verified"] is True
    assert second["status"] == third["status"] == "blocked"
    assert third["changes"]["coverage_baseline_snapshot_id"] == first["snapshot_id"]
    assert third["changes"]["missing_unexplained"] == second["changes"]["missing_unexplained"]
    assert "board_count_drop_unexplained:bse" in third["blockers"]


def test_unknown_metadata_enrichment_preserves_all_security_ids_and_first_seen(store):
    unverified = rows(10)
    for item in unverified:
        item["metadata_verified"] = False
    first = sync_universe(store, **inputs(unverified))
    second = sync_universe(store, **inputs(rows(10, day="2026-09-11"), day="2026-09-11"))
    assert {item["security_id"] for item in first["members"]} == {item["security_id"] for item in second["members"]}
    assert {item["first_seen_at"] for item in second["members"]} == {STAMP}
    assert second["structural_verified"] is True
    assert not second["changes"]["added"] and not second["changes"]["missing_unexplained"]


def test_later_unknown_metadata_does_not_forget_prior_identity(store):
    first = sync_universe(store, **inputs(rows(10)))
    unverified = rows(10, day="2026-09-11")
    for item in unverified:
        item["metadata_verified"] = False
    second = sync_universe(store, **inputs(unverified, day="2026-09-11"))
    assert {item["security_id"] for item in first["members"]} == {item["security_id"] for item in second["members"]}
    assert second["status"] == "blocked"


def test_multi_provider_conflicting_risk_states_become_unknown(store):
    data = inputs(rows(10))
    copy = deepcopy(data)
    for page in copy["pages"]:
        page["provider"] = "second_fixture"
        page["records"][0]["statuses"]["st"] = state(True)
    for manifest in copy["manifests"]:
        manifest["provider"] = "second_fixture"
        manifest["lineage_id"] = "second_fixture_exchange"
    data["pages"].extend(copy["pages"])
    data["manifests"].extend(copy["manifests"])
    result = sync_universe(store, **data)
    member = next(item for item in result["members"] if item["code"] == "synthetic_000000")
    assert member["statuses"]["st"]["value"] is None
    assert member["statuses"]["st"]["unknown_reason"] == "conflicting_verified_sources"
    assert result["status_verified"] is False and result["status"] == "blocked"
    assert result["ordinary_a_count"] == 10
    assert any(reason.startswith("conflicting_security_status") for reason in result["blockers"])


@pytest.mark.parametrize("kind", ["OFFLINE_TEST", "offline_test", "demo", "synthetic"])
def test_research_rejects_legacy_nonresearch_data_before_any_migration(tmp_path, kind):
    from ashare_daily.storage.market import MarketStore
    path = tmp_path / "legacy.sqlite3"
    MarketStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO market_metadata VALUES('verification_kind',?)", (kind,))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="provenance mode mismatch"):
        UniverseStore(path, mode="research")
    assert path.read_bytes() == before


def test_demo_database_rejected_before_any_migration(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE demo_reports(value TEXT)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="DEMO"):
        UniverseStore(path, mode="research")
    assert path.read_bytes() == before


def test_late_mapping_cannot_create_overlapping_code_identities(store):
    first = rows(10)
    first[4]["code"] = "old-bse"
    original = sync_universe(store, **inputs(first))
    third = rows(10, day="2026-09-12")
    third[4]["code"] = "new-bse"
    sync_universe(store, **inputs(third, day="2026-09-12"))
    fourth = rows(10, day="2026-09-13")
    fourth[4]["code"] = "new-bse"
    result = sync_universe(store, **inputs(fourth, day="2026-09-13"), alias_events=[alias_event(observed_at="2026-09-13T20:00:00+08:00")])
    assert any(reason.startswith("alias_conflict") for reason in result["blockers"])
    assert store.resolve_alias("fixture", "BSE", "new-bse", "2026-09-13") is not None
    assert store.get_snapshot(original["snapshot_id"]) == original


def test_late_mapping_can_fill_evidenced_alias_interval_before_first_observation(store):
    third = rows(10, day="2026-09-12")
    third[4]["code"] = "new-bse"
    original = sync_universe(store, **inputs(third, day="2026-09-12"))
    known_id = next(member["security_id"] for member in original["members"] if member["code"] == "new-bse")
    fourth = rows(10, day="2026-09-13")
    fourth[4]["code"] = "new-bse"
    result = sync_universe(store, **inputs(fourth, day="2026-09-13"), alias_events=[alias_event(old_valid_from="2026-01-01", observed_at="2026-09-13T20:00:00+08:00")])
    assert not result["blockers"]
    assert store.resolve_alias("fixture", "BSE", "old-bse", "2026-09-10") == known_id
    assert store.resolve_alias("fixture", "BSE", "new-bse", "2026-09-11") == known_id
    assert store.resolve_alias("fixture", "BSE", "new-bse", "2026-09-12") == known_id
    assert store.get_snapshot(original["snapshot_id"]) == original


@pytest.mark.parametrize("field", ["metadata_verified", "status_verified", "manifest_authoritative", "manifest_complete", "terminal", "calendar_verified"])
def test_string_boolean_cannot_upgrade_evidence_or_completeness(store, field):
    data = inputs(rows(10))
    if field == "metadata_verified":
        data["pages"][0]["records"][0]["metadata_verified"] = "false"
    elif field == "status_verified":
        data["pages"][0]["records"][0]["statuses"]["st"]["verified"] = "false"
    elif field.startswith("manifest_"):
        data["manifests"][0][field.removeprefix("manifest_")] = "false"
    elif field == "terminal":
        data["pages"][-1]["terminal"] = "false"
    else:
        data["calendar_verified"] = "false"
    result = sync_universe(store, **data)
    assert result["status"] == "blocked" or result["status_verified"] is False


def test_corrupt_snapshot_cannot_become_next_day_baseline(store):
    original = sync_universe(store, **inputs(rows(10)))
    store.connection.execute("DROP TRIGGER f1_snapshot_no_update")
    payload = deepcopy(original)
    payload["board_counts"]["bse"] = 0
    with store.connection:
        store.connection.execute("UPDATE f1_universe_snapshots SET payload=?", (json.dumps(payload),))
    with pytest.raises(ValueError, match="hash mismatch"):
        store.get_snapshot(original["snapshot_id"])
    with pytest.raises(ValueError, match="hash mismatch"):
        sync_universe(store, **inputs(rows(10, day="2026-09-11"), day="2026-09-11"))


def test_schema_migration_failure_rolls_back_new_tables_and_keeps_legacy(tmp_path):
    from ashare_daily.storage.market import MarketStore
    path = tmp_path / "legacy.sqlite3"
    MarketStore(path)
    with sqlite3.connect(path) as connection:
        # Deliberate schema collision forces an error after the first new DDL.
        connection.execute("CREATE TABLE f1_security_aliases(incompatible TEXT)")
        before = connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    with pytest.raises(sqlite3.OperationalError):
        UniverseStore(path, mode="research")
    with sqlite3.connect(path) as connection:
        after = connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    assert after == before


def test_offline_test_cannot_write_or_reopen_research_paths(tmp_path):
    with pytest.raises(ValueError, match="research directory"):
        UniverseStore(tmp_path / "research" / "universe.sqlite3", mode="offline_test")
    path = tmp_path / "isolated.sqlite3"
    with UniverseStore(path, mode="research") as target:
        with pytest.raises(ValueError, match="online"):
            sync_universe(target, **inputs())
    with pytest.raises(ValueError, match="provenance mode mismatch"):
        UniverseStore(path, mode="offline_test")


def test_online_manifest_cannot_launder_fixture_rows_into_research(tmp_path):
    data = inputs()
    data["manifests"][0]["provenance_mode"] = "online"
    with UniverseStore(tmp_path / "universe.sqlite3") as target:
        with pytest.raises(ValueError, match="synthetic"):
            sync_universe(target, **data)


def test_10000_synthetic_discoveries_scale_without_truncation(tmp_path):
    records = rows(10_000)
    data = inputs(records)
    tracemalloc.start()
    started = time.perf_counter()
    path = tmp_path / "offline_test" / "scale.sqlite3"
    with UniverseStore(path, mode="offline_test") as target:
        result = sync_universe(target, **data)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    metrics = {"mode": "offline_test", "synthetic_records": 10_000, "pages": len(data["pages"]),
               "network_requests": 0, "network_wait_seconds": 0, "model_calls": 0,
               "elapsed_seconds": round(elapsed, 3), "tracemalloc_peak_bytes": peak,
               "sqlite_bytes": path.stat().st_size}
    (tmp_path / "synthetic_scale_metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    print("F1_SYNTHETIC_SCALE=" + json.dumps(metrics, sort_keys=True))
    assert result["ordinary_a_count"] == 10_000
    assert result["board_counts"] == dict.fromkeys(BOARDS, 2000)
    assert result["discovered_unique"] == 10_000
    assert not result["blockers"]
    assert elapsed < 120  # Generous regression ceiling; not a real-network SLA.
