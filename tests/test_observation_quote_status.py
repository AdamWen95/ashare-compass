"""Offline protocol fixtures for the production zero-quote bridge.

The production entry expects research envelopes. These synthetic envelopes live
only in pytest temporary directories; the SDK class is always replaced, so no
fixture is fetched online, imported into a research database, or published.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from ashare_daily.calendar import _digest
from ashare_daily.operations import observation as workflow
from ashare_daily.providers import baostock as bs
from ashare_daily.providers import baostock_f2
from ashare_daily.sector_selection import digest, evaluate, verify_selection
from test_sector_status import DAY, STAMP, fixture as source_fixture, sdk, write


@pytest.fixture
def bridge(source_fixture, monkeypatch):
    root, universe, quotes, folder, manifest, _ = source_fixture
    universe, quotes = deepcopy(universe), deepcopy(quotes)
    universe.update(mode="research", ordinary_a_count=len(universe["members"]),
        content_hash="synthetic-protocol-only", observed_at=STAMP, source_manifests=[])
    for member in universe["members"]:
        member.update(provenance_mode="online", name="OFFLINE_TEST", listing_date="2000-01-01", listing_status="listed")
    quotes["provenance_mode"] = "online"
    for row in quotes["rows"]:
        row.update(date_verified=True, quote_at=DAY + "T15:00:01+08:00", reference_price="10",
            change_pct=None, amount_cny="0", percentage_basis="source_reference_price", amount_unit="CNY", price_unit="CNY/share")
    normal(quotes["rows"][1])
    calendar = json.loads(Path(manifest["calendar"]["source_path"]).read_text(encoding="utf-8"))
    calendar.pop("content_hash")
    calendar.update(mode="research", fixture_notice="OFFLINE_TEST protocol only")
    calendar["content_hash"] = _digest(calendar)
    calpath = folder / "bridge-calendar.json"
    calhash = write(calpath, calendar)
    packet = {"universe": universe, "quotes": quotes,
        "calendar": {"calendar_verified": True, "calendar": {DAY: True}, "source_path": str(calpath),
            "source_file_hash": calhash, "source_raw_hash": calendar["response"]["raw_hash"]},
        "catalog": {"provenance_mode": "online", "status": "ok", "complete": True, "boundary_verified": True,
            "rows": [{"sector_id": "fixture-industry", "taxonomy": "sina_industry", "kind": "industry", "name": "OFFLINE_TEST"}]},
        "memberships": {"fixture-industry": {"provenance_mode": "online", "complete": True, "boundary_verified": True,
            "rows": [{"symbol": row["symbol"]} for row in quotes["rows"]]}}}
    config = json.loads((Path(__file__).resolve().parents[1] / "config/sector_first.json").read_text(encoding="utf-8"))
    config.update(taxonomy="sina_industry", output_directory="outputs/offline_test/quote_bridge")
    monkeypatch.setattr(bs.BaoStockClient, "query", lambda *args, **kwargs: pytest.fail("real SDK/network must not run"))
    monkeypatch.setattr(workflow.elapsed, "sleep", lambda _: None)
    return root, packet, config


def normal(row):
    row.update(close="10.2", reference_price="10", change_pct="2", amount_cny="60000000", issues=[])


def fake_sdk(monkeypatch, response_factory=None, after_query=None):
    calls, instances = [], []
    class Client:
        def __init__(self, **kwargs):
            self.timeout_seconds = kwargs["timeout_seconds"]
            self.options = kwargs
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def query(self, operation, **parameters):
            calls.append((operation, parameters, self.timeout_seconds))
            result = (response_factory or sdk)(parameters["code"])
            result.update(mode="research", provenance_mode="online", verification_kind="live_network",
                fixture_notice="OFFLINE_TEST protocol response; no live request")
            if after_query:
                after_query()
            return result
    monkeypatch.setattr(baostock_f2, "BaoStockF2Client", Client)
    return calls, instances


def selected(packet, config):
    return evaluate(packet["universe"], packet["catalog"], packet["memberships"], packet["quotes"],
        target=DAY, cutoff=workflow.datetime.now(workflow.SHANGHAI).isoformat(), config=config, calendar=packet["calendar"])[0]


def supplement(bridge, *, max_seconds=10, max_queries=10):
    root, packet, _ = bridge
    return workflow.supplement_zero_quotes(root, packet, DAY, root / "offline_test/bridge-status",
        max_seconds=max_seconds, max_queries=max_queries)


@pytest.mark.parametrize("problem", ["wrong_date", "unverified_date", "normal"])
def test_wrong_or_unverified_date_and_normal_quote_never_query(bridge, monkeypatch, problem):
    _, packet, _ = bridge
    row = packet["quotes"]["rows"][0]
    if problem == "wrong_date":
        row["trade_date"] = "2026-09-09"
    elif problem == "unverified_date":
        row["date_verified"] = False
    else:
        normal(row)
    calls, instances = fake_sdk(monkeypatch)
    assert supplement(bridge) is packet
    assert not calls and not instances


def test_dated_zero_quote_becomes_rankable_only_after_verified_halt_proof(bridge, monkeypatch):
    _, packet, config = bridge
    original = deepcopy(packet)
    assert selected(packet, config)["selection_status"] == "selection_blocked"
    calls, _ = fake_sdk(monkeypatch)
    updated = supplement(bridge)
    assert packet == original
    assert [call[1]["code"] for call in calls] == ["sz.000001"]
    assert calls[0][0] == "history_f2"
    assert calls[0][1]["start_date"] == calls[0][1]["end_date"] == DAY
    proof = updated["quotes"]["rows"][0]["full_day_halt_evidence"]
    assert proof["verified"] is proof["full_day"] is proof["value"] is True
    assert proof["as_of_date"] == DAY and proof["observed_at"] == STAMP
    result = selected(updated, config)
    assert result["selection_status"] == "selected" and result["selection_verified"] is True
    assert result["sectors"][0]["expected_quote_count"] == result["sectors"][0]["valid_quote_count"] == 1
    assert result["selected_security_count"] == 2  # A halt changes Q, never erases a member of S.
    assert updated["quotes"]["rows"][0]["close"] is None
    assert updated["quotes"]["rows"][0]["is_st"] is None


def test_st_name_or_st_flag_is_not_used_to_infer_a_full_day_halt(bridge, monkeypatch):
    _, packet, config = bridge
    packet["universe"]["members"][0]["name"] = "*ST OFFLINE_TEST"
    packet["quotes"]["rows"][0]["is_st"] = True
    def trading(symbol):
        result = sdk(symbol, flag="1")
        result["rows"][0]["isST"] = "1"
        result["raw_hash"] = bs.raw_hash(result["fields"], result["rows"])
        return result
    calls, _ = fake_sdk(monkeypatch, trading)
    updated = supplement(bridge)
    assert len(calls) == 1
    assert "full_day_halt_evidence" not in updated["quotes"]["rows"][0]
    assert selected(updated, config)["selection_status"] == "selection_blocked"


@pytest.mark.parametrize("failure", ["permission_denied", "rate_limited", "timeout"])
def test_failed_source_stops_and_cannot_create_a_halt(bridge, monkeypatch, failure):
    root, packet, config = bridge
    packet["quotes"]["rows"][1].update(close=None, issues=["zero_price"])
    def failed(symbol):
        result = sdk(symbol)
        result.update(ok=False, status=failure, error_code="OFFLINE_TEST_" + failure, rows=[])
        result["raw_hash"] = bs.raw_hash(result["fields"], [])
        return result
    calls, _ = fake_sdk(monkeypatch, failed)
    updated = supplement(bridge)
    assert len(calls) == 1
    assert all("full_day_halt_evidence" not in row for row in updated["quotes"]["rows"])
    assert selected(updated, config)["selection_status"] == "selection_blocked"
    result = json.loads((root / "offline_test/bridge-status/result.json").read_text(encoding="utf-8"))
    assert result["network_requests"] == 1 and result["source_stop_reason"] == "status_source_failed"
    assert result["unresolved"] == ["sz.000001", "sz.000002"]


def test_query_count_limit_retains_unattempted_quotes_as_blockers(bridge, monkeypatch):
    root, packet, config = bridge
    packet["quotes"]["rows"][1].update(close=None, issues=["zero_price"])
    calls, _ = fake_sdk(monkeypatch)
    updated = supplement(bridge, max_queries=1)
    assert len(calls) == 1 and "full_day_halt_evidence" not in updated["quotes"]["rows"][1]
    assert selected(updated, config)["selection_status"] == "selection_blocked"
    manifest = json.loads((root / "offline_test/bridge-status/manifest.json").read_text(encoding="utf-8"))
    assert manifest["requested_count"] == manifest["unattempted_count"] == 1


def test_deadline_stops_before_second_request_and_preserves_unknown(bridge, monkeypatch):
    root, packet, config = bridge
    packet["quotes"]["rows"][1].update(close=None, issues=["zero_price"])
    elapsed = [0.0]
    monkeypatch.setattr(workflow.elapsed, "monotonic", lambda: elapsed[0])
    calls, _ = fake_sdk(monkeypatch, after_query=lambda: elapsed.__setitem__(0, 2.0))
    updated = supplement(bridge, max_seconds=1)
    assert len(calls) == 1 and calls[0][2] == 1
    assert "full_day_halt_evidence" not in updated["quotes"]["rows"][1]
    assert selected(updated, config)["selection_status"] == "selection_blocked"
    result = json.loads((root / "offline_test/bridge-status/result.json").read_text(encoding="utf-8"))
    assert result["source_stop_reason"] == "status_runtime_limit"


@pytest.mark.parametrize("mutation", [
    lambda response: response["rows"][0].update(date="2026-09-09"),
    lambda response: response["diagnostics"].update(events=[]),
])
def test_sdk_success_without_correct_day_and_reader_end_does_not_pass(bridge, monkeypatch, mutation):
    def invalid(symbol):
        response = sdk(symbol)
        mutation(response)
        response["raw_hash"] = bs.raw_hash(response["fields"], response["rows"])
        return response
    fake_sdk(monkeypatch, invalid)
    with pytest.raises(ValueError):
        supplement(bridge)


def test_selection_bridge_evaluates_and_archives_the_actual_overlay(bridge, monkeypatch):
    root, packet, config = bridge
    calls, _ = fake_sdk(monkeypatch)
    monkeypatch.setattr("ashare_daily.sector_pipeline.collect_inputs", lambda *args, **kwargs: deepcopy(packet))
    result = workflow.select_observation(root, config, DAY, "offline-bridge-run",
        {"max_quote_status_seconds": 5, "max_quote_status_queries": 10}, 10)
    assert len(calls) == 1
    assert verify_selection(result) == result and result["selection_status"] == "selected"
    directory = root / config["output_directory"] / result["selection_id"]
    archived = json.loads((directory / "source_inputs.json").read_text(encoding="utf-8"))
    assert archived["quotes"]["status_evidence_overlays"][0]["applied_security_ids"] == ["synthetic-status-1"]
    assert result["evidence_hashes"]["quotes"] == digest(archived["quotes"])
    assert datetime.fromisoformat(result["cutoff_at"]) >= datetime.fromisoformat(STAMP)
    assert not list(root.rglob("*.sqlite3"))


def test_selection_does_not_start_status_collection_after_source_budget_exhaustion(bridge, monkeypatch):
    root, packet, config = bridge
    elapsed = [0.0]
    monkeypatch.setattr(workflow.elapsed, "monotonic", lambda: elapsed[0])
    calls, instances = fake_sdk(monkeypatch)
    def collected(*args, **kwargs):
        elapsed[0] = 10.0
        return deepcopy(packet)
    monkeypatch.setattr("ashare_daily.sector_pipeline.collect_inputs", collected)
    result = workflow.select_observation(root, config, DAY, "offline-expired-run",
        {"max_quote_status_seconds": 5, "max_quote_status_queries": 10}, 10)
    assert not calls and not instances
    assert result["selection_status"] == "selection_blocked" and result["selection_verified"] is False
