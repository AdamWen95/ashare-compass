"""Offline fixtures for dated gap attribution; never request research data."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from ashare_daily import sector_gap_diagnosis as gap
from ashare_daily.providers import baostock as bs
from ashare_daily.providers import sina_history as sina
from test_sector_status import sdk, DAY, STAMP
from test_sector_history import (calendar, source_factory, validation_config, validation_freeze,
    member, DAYS, history)


def task():
    return {"security_id": "offline-gap-609", "code": "000609", "expected_dates": [DAY],
        "parameters": dict(code="sz.000609", start_date=DAY, end_date=DAY, security_type="stock", adjustment_mode="unadjusted")}


def row():
    return {"security_id": "offline-gap-609", "code": "000609", "date": DAY,
        "classification": "source_omitted_date_reason_unknown", "reason": "decoded_original_body_has_no_record",
        "definitive_non_trading": False, "historical_state": None}


def response():
    return sdk("sz.000609")


def test_explicit_halt_explains_date_without_inserting_or_forward_filling_price():
    rows = [row()]
    original = response()
    quality = gap._apply_response(rows, task(), original, {"path": "offline/source.json", "sha256": "1" * 64}, mode="offline_test")
    assert rows[0]["classification"] == "verified_non_trading_date"
    assert rows[0]["definitive_non_trading"] is True
    state = rows[0]["historical_state"]
    assert state["value"] is state["verified"] is state["full_day"] is True
    assert state["effective_from"] == state["effective_to"] == DAY
    assert state["observed_at"] == STAMP and state["published_at"] is None and state["historical_reconstruction"]
    assert "close" not in rows[0] and original == response()
    assert quality["suspended_dates"] == [DAY]


def test_actual_traded_row_is_source_disagreement_not_permission_to_mix_adjustments():
    source = response()
    source["rows"][0].update(tradestatus="1", volume="100", amount="1000")
    source["raw_hash"] = bs.raw_hash(source["fields"], source["rows"])
    rows = [row()]
    gap._apply_response(rows, task(), source, {"path": "offline/source.json", "sha256": "1" * 64}, mode="offline_test")
    assert rows[0]["classification"] == "source_disagreement_real_quote_available"
    assert not rows[0]["can_fill_from_current_evidence"] and not rows[0]["definitive_non_trading"]
    assert rows[0]["historical_state"]["value"] is False


def test_explicit_halt_with_empty_activity_retains_nulls_and_is_not_a_conflict():
    source = response()
    source["rows"][0].update(volume="", amount="")
    source["raw_hash"] = bs.raw_hash(source["fields"], source["rows"])
    rows = [row()]
    gap._apply_response(rows, task(), source, {"path": "offline/source.json", "sha256": "1" * 64}, mode="offline_test")
    assert rows[0]["definitive_non_trading"] and rows[0]["historical_state"]["value"] is True
    assert rows[0]["historical_state"]["conflict"] is False
    assert rows[0]["source_placeholder_activity"]["volume_raw"] == ""
    assert rows[0]["source_placeholder_activity"]["amount_raw"] == ""
    assert rows[0]["source_placeholder_activity"]["filled_values"] is False


@pytest.mark.parametrize("update", [{"tradestatus": ""}, {"tradestatus": "0", "volume": "100", "amount": "1000"}])
def test_unknown_or_contradictory_status_is_never_confirmed_halt(update):
    source = response()
    source["rows"][0].update(update)
    source["raw_hash"] = bs.raw_hash(source["fields"], source["rows"])
    rows = [row()]
    gap._apply_response(rows, task(), source, {"path": "offline/source.json", "sha256": "1" * 64}, mode="offline_test")
    assert not rows[0]["definitive_non_trading"] and rows[0]["historical_state"]["value"] is None


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(raw_hash="0" * 64),
    lambda r: r["parameters"].update(code="sh.600673"),
    lambda r: r["rows"][0].update(code="sh.600673"),
    lambda r: r["rows"][0].update(date="2026-09-09"),
    lambda r: r["rows"][0].update(adjustflag="2"),
    lambda r: r["rows"].append(deepcopy(r["rows"][0])),
    lambda r: r["login"].update(ok=False),
    lambda r: r["diagnostics"].update(hard_timeout=True),
    lambda r: r["diagnostics"]["events"][0].update(initial_page_records=2),
    lambda r: r["diagnostics"]["events"][1].update(row_count=0),
    lambda r: r.update(verification_kind="live_network"),
    lambda r: r.update(fetched_at="2099-01-01T21:00:00+08:00"),
])
def test_identity_date_protocol_provenance_and_terminal_fail_closed(mutate):
    source = response()
    mutate(source)
    with pytest.raises(ValueError):
        gap._validate_response(source, task(), "offline_test")


def test_missing_source_row_is_unknown_not_halt():
    source = response()
    source["rows"] = []
    source["status"] = "empty_confirmed"
    source["raw_hash"] = bs.raw_hash(source["fields"], [])
    for key in ("initial_page_records", "row_count"):
        for event in source["diagnostics"]["events"]:
            if key in event:
                event[key] = 0
    rows = [row()]
    gap._apply_response(rows, task(), source, {"path": "offline/source.json", "sha256": "1" * 64}, mode="offline_test")
    assert rows[0]["historical_state"] is None and rows[0]["definitive_non_trading"] is False
    assert rows[0]["reason"] == "both_verified_responses_omit_date_state_unknown"


def test_groups_use_trusted_calendar_not_calendar_day_or_weekday_rules():
    dates = ["2025-11-12", "2025-11-13", "2025-11-14", "2025-11-17", "2025-11-18", "2025-11-19"]
    assert gap._group_dates(["2025-11-19", "2025-11-13", "2025-11-14", "2025-11-17"], dates) == [
        ["2025-11-13", "2025-11-14", "2025-11-17"], ["2025-11-19"]]
    with pytest.raises(ValueError):
        gap._group_dates(["2025-11-15"], dates)


def test_offline_analysis_preserves_frozen_selection_database_and_unknown_state(tmp_path, monkeypatch):
    cfg, calls = validation_config(), []
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, calls)
    monkeypatch.setattr(sina, "decode_pinned", lambda _: [{"date": DAY, "open": "10", "high": "11", "low": "9",
        "close": "10.5", "volume": "1000", "amount": "10000"}])
    selected = [{**member(), "code": "000609", "board": "szse_main", "exchange": "SZSE"}]
    selection = validation_freeze(cfg, selected)
    history.prepare_history(tmp_path, selection, cfg)
    strategy = Path(__file__).resolve().parents[1] / "config/sector_screening.json"
    (tmp_path / "config").mkdir()
    (tmp_path / "config/sector_screening.json").write_bytes(strategy.read_bytes())
    original = deepcopy(selection)
    database = tmp_path / cfg["database"]
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    monkeypatch.setattr(gap, "BaoStockF2Client", lambda **k: pytest.fail("offline diagnosis started SDK"))
    result = gap.diagnose_history_gaps(tmp_path, selection, cfg)
    assert result["gap_date_count"] == 1 and result["rows"][0]["date"] == DAYS[0]
    assert result["metrics"]["network_requests"] == result["database_writes"] == result["model_calls"] == 0
    assert result["rows"][0]["sina_decoded_record_present"] is False
    assert result["rows"][0]["factor_present"] and result["rows"][0]["historical_state"] is None
    assert result["selection_id"] == selection["selection_id"] and selection == original
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    saved = history._read(Path(result["json_path"]))
    assert saved["content_hash"] == result["content_hash"]
    file_names = sorted(str(p) for p in history._io(tmp_path).rglob("*") if p.is_file())
    checked = gap.read_gap_diagnosis(tmp_path, result["json_path"], selection, cfg)
    assert checked["verified"] and checked["rows"] == result["rows"]
    assert sorted(str(p) for p in history._io(tmp_path).rglob("*") if p.is_file()) == file_names
    with pytest.raises(ValueError, match="test provenance"):
        gap.diagnose_history_gaps(tmp_path, selection, cfg, online=True)
    repeated = gap.diagnose_history_gaps(tmp_path, selection, cfg)
    assert repeated["revision_run_id"] != result["revision_run_id"] and repeated["rows"] == result["rows"]
    # A new isolated evidence revision can reuse the exact SDK bytes. The old
    # diagnosis and all original history/checkpoints remain immutable.
    old_result_body = history._io(Path(result["json_path"])).read_bytes()
    source_folder = Path(result["json_path"]).parent.parent / "offline-source-revision"
    manifest = history._read(Path(result["json_path"]).parent / "manifest.json")
    original_task = manifest["tasks"][0]
    source = sdk("sz.000609")
    source["parameters"] = original_task["parameters"]
    source["rows"][0].update(date=DAYS[0], volume="", amount="")
    source["raw_hash"] = bs.raw_hash(source["fields"], source["rows"])
    response_path = source_folder / "responses/01.json"
    response_hash = history._write_new(response_path, source)
    packet = {**deepcopy(result), "requests": [{"task": original_task, "path": str(response_path), "sha256": response_hash}],
        "file_refs": [{"path": str(response_path), "sha256": response_hash}]}
    packet.pop("content_hash")
    source_path = source_folder / "history_gap_diagnosis.json"
    history._write_new(source_path, history._seal(packet))
    replay = gap.diagnose_history_gaps(tmp_path, selection, cfg, source_revision=source_path)
    assert replay["status"] == "diagnosed" and replay["metrics"]["network_requests"] == 0
    assert replay["definitive_non_trading_dates_by_security"][member()["security_id"]] == [DAYS[0]]
    assert replay["rows"][0]["historical_state"]["observed_at"] == STAMP
    checked = gap.read_gap_diagnosis(tmp_path, replay["json_path"], selection, cfg)
    assert checked["verified"] and checked["definitive_non_trading_dates_by_security"] == replay["definitive_non_trading_dates_by_security"]
    assert history._io(Path(result["json_path"])).read_bytes() == old_result_body
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    replay_path = Path(replay["json_path"])
    honest = history._io(replay_path).read_bytes()
    for change in (
        lambda value: value["rows"][0].update(definitive_non_trading=False),
        lambda value: value["definitive_non_trading_dates_by_security"].update({member()["security_id"]: []}),
        lambda value: value["securities"][0].update(cache_target_complete=True),
        lambda value: value.update(file_refs=[]),
        lambda value: value.update(selection_content_hash="0" * 64),
    ):
        forged = deepcopy(replay)
        change(forged)
        forged.pop("content_hash")
        history._io(replay_path).write_text(json.dumps(history._seal(forged)), encoding="utf-8")
        with pytest.raises(ValueError):
            gap.read_gap_diagnosis(tmp_path, replay_path, selection, cfg)
        history._io(replay_path).write_bytes(honest)
    with history._io(response_path).open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="hash"):
        gap.diagnose_history_gaps(tmp_path, selection, cfg, source_revision=source_path)
    with pytest.raises(ValueError, match="hash"):
        gap.read_gap_diagnosis(tmp_path, replay_path, selection, cfg)
