"""Multiple-source storage contracts from isolated synthetic HTTP/SDK evidence.

No live service, permissions, status normality or production coverage is implied.
"""
from base64 import b64encode
from dataclasses import asdict, replace
from hashlib import sha256
import json
import sqlite3

import pytest

from ashare_daily.market_foundation import F2MarketStore
from ashare_daily.providers.base import DailyBarRequest, SecurityIdentity
from ashare_daily.providers.eastmoney import history_parameters
from test_f2_market_foundation import (
    DAY, PREVIOUS, SID, STAMP, SYMBOL, count, response as bao_response,
    row as bao_row, save as bao_save,
)


IDENTITY = SecurityIdentity(SID, "688001", "SSE", "star", metadata_verified=True)
ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def request(*, dates=(DAY,), adjustment="unadjusted"):
    return DailyBarRequest(IDENTITY, min(dates), max(dates), tuple(sorted(dates)), adjustment)


def kline(day=DAY, *, close="10", volume="123", amount="123000"):
    return ",".join((day, "10", close, "11", "9", volume, amount, "20", "0", "0", "2"))


def em_response(req=None, *, lines=None, stamp=STAMP):
    """A literal HTTP archive, never a mocked normalization result."""
    req = req or request()
    payload = {"rc": 0, "rt": 17, "svr": 0, "lt": 1, "full": 1,
               "data": {"code": IDENTITY.code, "market": 1, "name": "离线样本",
                        "decimal": 2, "dktotal": 1,
                        "klines": [kline(day) for day in req.expected_dates] if lines is None else lines}}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf8")
    params = history_parameters(req)
    http = {"schema_version": "bounded-http-response-v1", "request": {"url": ENDPOINT, "parameters": params},
            "http_status": 200, "body_base64": b64encode(body).decode("ascii"),
            "body_sha256": sha256(body).hexdigest(), "body_complete": True, "fetched_at": stamp,
            "provenance_mode": "offline_test", "verification_kind": "offline_test",
            "ok": True, "status": "ok", "error_code": "0", "error_msg": "", "metrics": {"requests": 1, "retries": 0}}
    return {"schema_version": "f2-eastmoney-response-v1", "provider": "eastmoney", "operation": "daily_bars",
            "identity": asdict(req.identity), "security_id": req.identity.security_id, "symbol": req.identity.symbol,
            "scope": req.identity.scope, "parameters": req.parameters(), "expected_dates": list(req.expected_dates),
            "fetched_at": stamp, "source_endpoint": ENDPOINT, "source_symbol": req.identity.source_symbol("eastmoney"),
            "source_business_date": None, "provenance_mode": "offline_test", "verification_kind": "offline_test",
            "ok": True, "status": "ok", "error_code": "0", "error_msg": "", "http": http,
            "raw_hash": http["body_sha256"], "payload": payload}


@pytest.fixture
def store(tmp_path):
    return F2MarketStore(tmp_path / "offline_test" / "market.sqlite3", mode="offline_test")


def save_em(store, data=None, *, req=None, **updates):
    req = req or request()
    data = em_response(req) if data is None else data
    body = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf8")
    digest = sha256(body).hexdigest()
    source = store.path.parent / (digest + ".json")
    source.write_bytes(body)
    args = {"security_id": SID, "symbol": SYMBOL, "scope": "sse_szse_a",
            "universe_snapshot_id": "fixture-universe-v1", "response": data,
            "source_response_path": source, "source_response_hash": digest,
            "trading_dates": req.expected_dates, "provenance_mode": "offline_test",
            "adjustment_mode": req.adjustment_mode, "provider": "eastmoney", "request": req, **updates}
    return store.save_batch(**args)


def dump(store):
    with sqlite3.connect(store.path) as db:
        return tuple(db.iterdump())


def test_two_sources_same_security_date_are_isolated_and_default_remains_baostock(store):
    bao_save(store)
    save_em(store, em_response(lines=[kline(close="10.5")]))
    assert count(store, "f2_bar_current") == count(store, "f2_bar_versions") == 2
    bao = store.read_bars(SID, DAY, DAY)
    em = store.read_bars(SID, DAY, DAY, provider="eastmoney")
    assert len(bao) == len(em) == 1
    assert (bao[0]["provider"], bao[0]["close"]) == ("baostock", "10")
    assert (em[0]["provider"], em[0]["close"]) == ("eastmoney", "10.5")
    assert em[0]["volume_shares"] == 12300 and em[0]["amount_cny"] == "123000"
    assert store.get_bar_version(SID, DAY, em[0]["fact_hash"]) is None
    assert store.get_bar_version(SID, DAY, em[0]["fact_hash"], provider="eastmoney") == em[0]
    assert store.get_bar_version(SID, DAY, bao[0]["fact_hash"], provider="eastmoney") is None


def test_eastmoney_unknown_status_facts_never_borrow_baostock_normality(store):
    bao_save(store)
    saved = save_em(store)
    fact = store.read_bars(SID, DAY, DAY, provider="eastmoney")[0]
    assert fact["tradestatus"] is None and fact["is_st"] is None and fact["preclose"] is None
    assert not saved["quality"]["quote_complete"] and not saved["quality"]["status_complete"]
    assert saved["quality"]["trading_status_unknown_dates"] == [DAY]
    assert saved["quality"]["valid_quote_dates"] == []
    assert store.stored_dates(SID, DAY, DAY, provider="eastmoney") == set()
    assert store.stored_dates(SID, DAY, DAY) == {DAY}


def test_em_exact_replay_is_idempotent_and_preserves_byte_and_source_provenance(store):
    data = em_response()
    first = save_em(store, data)
    before = dump(store)
    second = save_em(store, data)
    assert dump(store) == before
    assert second["batch_id"] == first["batch_id"] and second["unchanged"] == 1
    with sqlite3.connect(store.path) as db:
        entry = db.execute("SELECT source_file_hash,source_raw_hash,provenance_json FROM f2_batches").fetchone()
    provenance = json.loads(entry[2])
    assert entry[:2] == (first["source_file_hash"], data["http"]["body_sha256"])
    assert entry[0] != entry[1]
    assert provenance["provider"] == "eastmoney" and provenance["source_endpoint"] == ENDPOINT
    assert provenance["source_symbol"] == "1.688001" and provenance["source_business_date"] is None
    assert provenance["quality_rules_version"] == first["quality"]["quality_rules_version"]


def test_em_correction_preserves_both_versions_and_does_not_update_baostock(store):
    bao_save(store)
    save_em(store)
    old = store.read_bars(SID, DAY, DAY, provider="eastmoney")[0]
    revised = save_em(store, em_response(lines=[kline(close="10.5")], stamp="2026-09-10T22:00:00+08:00"))
    assert revised["updated"] == 1 and count(store, "f2_bar_versions") == 3
    assert store.get_bar_version(SID, DAY, old["fact_hash"], provider="eastmoney") == old
    assert store.read_bars(SID, DAY, DAY, provider="eastmoney")[0]["close"] == "10.5"
    assert store.read_bars(SID, DAY, DAY)[0]["close"] == "10"


def test_em_later_equivalent_observation_does_not_create_a_business_version(store):
    first = save_em(store)
    original = store.read_bars(SID, DAY, DAY, provider="eastmoney")[0]
    repeated = save_em(store, em_response(lines=[kline(close="10.00")], stamp="2026-09-10T22:00:00+08:00"))
    assert repeated["batch_id"] != first["batch_id"] and repeated["unchanged"] == 1
    assert count(store, "f2_bar_versions") == 1 and count(store, "f2_bar_observations") == 2
    assert store.read_bars(SID, DAY, DAY, provider="eastmoney")[0] == original


def test_em_quality_version_upgrade_adds_audit_without_rewriting_facts_or_old_batch(store, monkeypatch):
    first = save_em(store)
    with sqlite3.connect(store.path) as db:
        frozen = db.execute("SELECT * FROM f2_batches WHERE batch_id=?", (first["batch_id"],)).fetchone()
    monkeypatch.setattr("ashare_daily.providers.eastmoney.QUALITY_RULES_VERSION", "synthetic-em-future-quality-v2")
    revised = save_em(store)
    assert revised["batch_id"] != first["batch_id"] and revised["unchanged"] == 1
    assert count(store, "f2_bar_versions") == 1 and count(store, "f2_batches") == 2
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT * FROM f2_batches WHERE batch_id=?", (first["batch_id"],)).fetchone() == frozen
    before = dump(store)
    assert save_em(store)["batch_id"] == revised["batch_id"]
    assert dump(store) == before


@pytest.mark.parametrize("stamp", [STAMP, "2026-09-10T20:00:00+08:00"])
def test_em_conflicting_same_time_or_older_correction_rolls_back_batch(store, stamp):
    save_em(store)
    before = dump(store)
    with pytest.raises(ValueError, match="same-time|older observation"):
        save_em(store, em_response(lines=[kline(close="10.5")], stamp=stamp))
    assert dump(store) == before


@pytest.mark.parametrize("change", [
    lambda data: data.update(raw_hash="0" * 64),
    lambda data: data["http"].update(body_sha256="0" * 64),
    lambda data: data["http"].update(body_base64=b64encode(b"{}").decode("ascii")),
    lambda data: data["payload"]["data"].update(klines=[kline(close="10.5")]),
    lambda data: data["http"].update(body_complete=False),
])
def test_em_http_bytes_hash_payload_and_completion_are_revalidated(store, change):
    data = em_response()
    change(data)
    with pytest.raises(ValueError):
        save_em(store, data)
    assert count(store, "f2_batches") == count(store, "f2_bar_versions") == 0


def test_em_archive_file_hash_is_required_in_addition_to_http_body_hash(store):
    with pytest.raises(ValueError, match="file/hash"):
        save_em(store, source_response_hash="0" * 64)
    assert count(store, "f2_batches") == 0


@pytest.mark.parametrize("change", [
    lambda data: data["identity"].update(security_id="another-security"),
    lambda data: data["identity"].update(metadata_verified=False),
    lambda data: data["identity"].update(board="sse_main"),
    lambda data: data.update(scope="all_a"),
    lambda data: data.update(source_symbol="0.688001"),
    lambda data: data.update(symbol="sz.688001"),
    lambda data: data.update(expected_dates=[PREVIOUS, DAY]),
    lambda data: data["parameters"].update(adjustment_mode="forward_adjusted"),
    lambda data: data.update(provider="baostock"),
])
def test_em_request_identity_scope_dates_adjustment_and_provider_cannot_change(store, change):
    data = em_response()
    change(data)
    with pytest.raises(ValueError):
        save_em(store, data)
    assert count(store, "f2_batches") == 0


def test_em_can_reconstruct_strict_request_from_archive_without_optional_object(store):
    saved = save_em(store, request=None, trading_dates={DAY})
    assert saved["inserted"] == 1


def test_no_unknown_or_implicit_all_source_reading(store):
    bao_save(store)
    for provider in (None, "", "all", "unknown"):
        with pytest.raises(ValueError, match="provider"):
            store.read_bars(SID, DAY, DAY, provider=provider)
        with pytest.raises(ValueError, match="provider"):
            store.stored_dates(SID, DAY, DAY, provider=provider)
        with pytest.raises(ValueError, match="provider"):
            store.get_bar_version(SID, DAY, "0" * 64, provider=provider)


def test_em_adjusted_facts_remain_audited_but_unknown_status_cannot_create_usable_window(store):
    req = request(adjustment="forward_adjusted")
    saved = save_em(store, req=req)
    assert len(saved["quality"]["records"]) == 1 and saved["window_id"] is None
    assert count(store, "f2_batches") == 1 and count(store, "f2_adjustment_windows") == 0
    assert store.read_bars(SID, DAY, DAY, provider="eastmoney") == []


def test_em_adjusted_response_never_patches_a_baostock_window_or_another_response(store):
    bao = bao_save(store, bao_response([bao_row(date=PREVIOUS, adjustflag="2"), bao_row(adjustflag="2")],
                                    adjusted=True, start=PREVIOUS), days=[PREVIOUS, DAY])
    original = store.get_adjustment_window(bao["window_id"])
    req = request(dates=(PREVIOUS, DAY), adjustment="forward_adjusted")
    a = save_em(store, em_response(req, lines=[kline(PREVIOUS)]), req=req)
    b = save_em(store, em_response(req, lines=[kline(DAY)], stamp="2026-09-10T22:00:00+08:00"), req=req)
    assert a["quality"]["missing_dates"] == [DAY] and b["quality"]["missing_dates"] == [PREVIOUS]
    assert a["window_id"] is b["window_id"] is None
    assert count(store, "f2_adjustment_windows") == 1
    assert store.get_adjustment_window(bao["window_id"]) == original
    assert {record["provider"] for record in original["records"]} == {"baostock"}


def test_em_adjusted_segment_cannot_claim_required_full_window(store):
    with pytest.raises(ValueError):
        save_em(store, em_response(request(adjustment="forward_adjusted")),
                req=request(dates=(PREVIOUS, DAY), adjustment="forward_adjusted"))
    assert count(store, "f2_batches") == count(store, "f2_adjustment_windows") == 0


@pytest.mark.parametrize("change", [
    lambda data: data.update(provenance_mode="online", verification_kind="live_network"),
    lambda data: data["http"].update(provenance_mode="online", verification_kind="live_network"),
])
def test_em_online_and_offline_provenance_never_mix(store, change):
    data = em_response()
    change(data)
    with pytest.raises(ValueError):
        save_em(store, data)
    assert count(store, "f2_batches") == 0


def test_em_fixture_cannot_enter_research_database(tmp_path):
    target = F2MarketStore(tmp_path / "isolated.sqlite3", mode="research")
    with pytest.raises(ValueError, match="provenance"):
        save_em(target)
    with pytest.raises(ValueError, match="provenance"):
        save_em(target, provenance_mode="online")
    assert count(target, "f2_batches") == 0


def test_em_future_observation_cannot_be_accepted_even_with_valid_http_hash(store):
    with pytest.raises(ValueError, match="future"):
        save_em(store, em_response(stamp="2100-01-01T21:00:00+08:00"))
    assert count(store, "f2_batches") == 0


def test_em_reusing_existing_batch_identifier_cannot_overwrite_frozen_inputs(store):
    first = save_em(store)
    before = dump(store)
    with pytest.raises(ValueError, match="batch_id conflicts"):
        save_em(store, em_response(lines=[kline(close="10.5")], stamp="2026-09-10T22:00:00+08:00"), batch_id=first["batch_id"])
    assert dump(store) == before


def test_explicit_provider_cannot_relabel_a_baostock_response(store):
    with pytest.raises(ValueError, match="provider"):
        bao_save(store, provider="eastmoney")
    assert count(store, "f2_batches") == 0


def test_optional_frozen_request_is_checked_for_legacy_baostock_input(store):
    wrong = replace(request(), identity=replace(IDENTITY, security_id="another-security"))
    with pytest.raises(ValueError, match="frozen request"):
        bao_save(store, request=wrong)
    assert count(store, "f2_batches") == 0
