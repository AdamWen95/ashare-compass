"""Offline contracts: no fixture in this file is evidence of live acceptance."""
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.providers.exchange_cache import CachedWebsiteTransport
from ashare_daily.providers.exchange_universe import SSE_METADATA_ENDPOINT, SSE_ENDPOINT
from ashare_daily.universe_service import SourcePermission, UniverseConfig, sync_date
from ashare_daily.universe_acceptance import write_acceptance_details

ROOT = Path(__file__).resolve().parents[1]


def test_approved_exchange_needs_three_explicit_site_registrations():
    config = json.loads((ROOT / "config/universe_f11.json").read_text(encoding="utf-8"))
    assert UniverseConfig.model_validate(config).sources[1].sites[2].access_status == "access_denied"
    config["sources"][1]["sites"].pop()
    with pytest.raises(ValueError, match="全部三站"):
        UniverseConfig.model_validate(config)


def test_no_implicit_account_permission_or_model_export():
    source = dict(provider="tushare", enabled=True, permission_status="approved", purpose="offline", permission_basis="token")
    with pytest.raises(ValueError, match="适配器"):
        SourcePermission.model_validate(source)
    source.update(provider="baostock", llm_export=True)
    with pytest.raises(ValueError):
        SourcePermission.model_validate(source)


def test_exchange_transport_cannot_be_injected_into_live_sync(tmp_path):
    with pytest.raises(ValueError, match="offline_test"):
        sync_date(project=tmp_path, config_path="absent.json", target=date.today(), exchange_transport=object())
    assert list(tmp_path.iterdir()) == []


def cache_fixture(tmp_path, monkeypatch):
    """Synthesized contract bytes only in pytest tmp directories."""
    now = datetime.now(SHANGHAI)
    params = {"sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GPGK_GSGK_C", "COMPANY_CODE": "OFFLINE"}
    raw = b'{"result":[{"SEC_TYPE":"OFFLINE"}]}'
    raw_path = tmp_path / "fixture.raw"
    raw_path.write_bytes(raw)
    response = {"ok": True, "verification_kind": "live_network", "provenance_mode": "online", "http_status": 200,
                "fetched_at": now.isoformat(), "url": SSE_METADATA_ENDPOINT + "?" + urlencode(params), "params": params,
                "target_date": now.date().isoformat(), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "raw_path": str(raw_path), "body": json.loads(raw)}
    calls = []
    class Transport:
        def __init__(self, *a, **k): pass
        def __call__(self, *a, **k):
            calls.append(k)
            return deepcopy(response)
    monkeypatch.setattr("ashare_daily.providers.exchange_cache.WebsiteTransport", Transport)
    cache = CachedWebsiteTransport(tmp_path / "run", cache_root=tmp_path / "cache")
    return cache, params, now.date(), calls


def test_cache_reuses_only_type_response_and_retains_original_observation(tmp_path, monkeypatch):
    cache, params, day, calls = cache_fixture(tmp_path, monkeypatch)
    first = cache(SSE_METADATA_ENDPOINT, params, label="first", target=day)
    second = cache(SSE_METADATA_ENDPOINT, params, label="again", target=day)
    assert len(calls) == 1 and second["cached"] is True
    assert second["fetched_at"] == first["fetched_at"]
    assert Path(second["raw_path"]).read_bytes() == Path(first["raw_path"]).read_bytes()
    cache(SSE_ENDPOINT, params, label="list", target=day)
    cache(SSE_ENDPOINT, params, label="list2", target=day)
    assert len(calls) == 3


@pytest.mark.parametrize("corruption", ["offline_mode", "raw", "body", "date", "url", "future", "permission"])
def test_untrusted_or_stale_cache_is_not_reused(tmp_path, monkeypatch, corruption):
    cache, params, day, calls = cache_fixture(tmp_path, monkeypatch)
    cache(SSE_METADATA_ENDPOINT, params, label="first", target=day)
    path = next((tmp_path / "cache").rglob("*.json"))
    packet = json.loads(path.read_text(encoding="utf-8"))
    r = packet["response"]
    if corruption == "offline_mode": packet["provenance_mode"] = "offline_test"
    elif corruption == "raw": packet["raw_hex"] = b'changed'.hex()
    elif corruption == "body": r["body"] = {"result": []}
    elif corruption == "date": r["target_date"] = "2000-01-01"
    elif corruption == "url": r["url"] = "https://unrelated.invalid/"
    elif corruption == "future": r["fetched_at"] = "2099-01-01T22:00:00+08:00"
    elif corruption == "permission": r["http_status"] = 403
    path.write_text(json.dumps(packet), encoding="utf-8")
    assert not cache(SSE_METADATA_ENDPOINT, params, label="again", target=day).get("cached")
    assert len(calls) == 2


def test_board_counts_and_state_completeness_are_independent(tmp_path):
    snapshot = {"snapshot_id": "OFFLINE", "requested_date": "2026-09-11", "calendar_verified": True,
                "universe_verified": False, "members": [], "changes": {"missing_unexplained": []},
                "blockers": ["source_failed:bse:listed_shares", "board_empty:bse"],
                "board_counts": dict(sse_main=1701, star=0, szse_main=1, chinext=1, bse=0),
                "source_manifests": [dict(provider="sse", dataset="main_a", coverage_boards=["sse_main"],
                                         complete=True, authoritative=True, permission_status="approved", errors=[]),
                                     dict(provider="bse", dataset="listed_shares", coverage_boards=["bse"],
                                          complete=False, authoritative=True, permission_status="approved", errors=["access_denied"])]}
    result = write_acceptance_details(tmp_path, snapshot)
    assert result["board_acceptance"]["sse_main"]["ordinary_a_count"] == 1701
    assert result["board_acceptance"]["bse"]["ordinary_a_count"] is None
    detail = json.loads(Path(result["acceptance_details_path"]).read_text(encoding="utf-8"))
    assert detail["price_completeness"] == "not_verified_F2"
    assert detail["research_eligibility_completeness"] == "not_verified"
    snapshot["blockers"].append("previous_members_missing_unexplained:1")
    assert write_acceptance_details(tmp_path, snapshot)["board_acceptance"]["sse_main"]["ordinary_a_count"] is None


def test_new_configuration_preserves_all_operational_and_model_budgets():
    old = json.loads((ROOT / "config/full_market_daily.json").read_text(encoding="utf-8"))
    new = json.loads((ROOT / "config/full_market_daily_f11.json").read_text(encoding="utf-8"))
    assert {k: v for k, v in new.items() if k != "universe_config"} == {k: v for k, v in old.items() if k != "universe_config"}


def test_bse_listing_date_cannot_substitute_for_conversion_evidence(tmp_path):
    from ashare_daily.universe import UniverseStore
    event = dict(provider="bse", exchange="BSE", old_code="OFFLINE-OLD", new_code="OFFLINE-NEW",
                 list_date="2020-01-01", verified=True, evidence_source="OFFLINE mapping fixture",
                 evidence_id="OFFLINE", observed_at="2026-09-11T18:00:00+08:00")
    with UniverseStore(tmp_path / "offline.sqlite3", mode="offline_test") as store:
        with pytest.raises(ValueError, match="mapping evidence"):
            store._apply_alias_event(event, "2026-09-11", "2026-09-11T20:00:00+08:00", "2026-09-11T20:00:00+08:00")
        assert store.resolve_alias("bse", "BSE", "OFFLINE-NEW", "2026-09-11") is None


def test_bse_distinct_conversion_batches_keep_distinct_effective_dates(tmp_path):
    from ashare_daily.universe import UniverseStore
    with UniverseStore(tmp_path / "offline.sqlite3", mode="offline_test") as store:
        for batch, effective in [("pilot", "2025-05-06"), ("bulk", "2025-10-09")]:
            event = dict(provider="bse", exchange="BSE", old_code="OFFLINE-old-" + batch,
                         new_code="OFFLINE-new-" + batch, effective_date=effective,
                         old_valid_from="2024-01-01", verified=True, evidence_source="OFFLINE explicit batch evidence",
                         evidence_id="OFFLINE-" + batch, observed_at="2026-09-11T18:00:00+08:00")
            store._apply_alias_event(event, "2026-09-11", "2026-09-11T20:00:00+08:00", "2026-09-11T20:00:00+08:00")
            previous = store.resolve_alias("bse", "BSE", "OFFLINE-old-" + batch, "2024-01-01")
            assert previous == store.resolve_alias("bse", "BSE", "OFFLINE-new-" + batch, effective)
        assert store.resolve_alias("bse", "BSE", "OFFLINE-new-bulk", "2025-05-06") is None


def test_actual_f11_service_uses_exchange_metadata_with_isolated_test_provenance(tmp_path):
    from test_f11_exchange_universe import sse_body, sse_raw, szse_body, szse_raw, response
    config = json.loads((ROOT / "config/universe_f11.json").read_text(encoding="utf-8"))
    config.update(database="data/offline/universe.sqlite3", output_directory="outputs/offline",
                  calendar_cache="data/offline/calendar", exchange_metadata_cache=None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    calls = []
    def transport(endpoint, params, **kwargs):
        calls.append(endpoint)
        if endpoint == SSE_METADATA_ENDPOINT:
            return response({"result": [{"COMPANY_CODE": "900002", "A_STOCK_CODE": "900002", "SEC_TYPE": "科创A"}]})
        if endpoint == SSE_ENDPOINT:
            kind = params["STOCK_TYPE"]
            return response(sse_body([sse_raw("900001" if kind == "1" else "900002", kind)]))
        return response(szse_body([szse_raw("890001"), szse_raw("890002", "创业板")]))
    class NoBaoStockLists:
        def query(self, *a, **k):
            pytest.fail("supplementary lists must not query failed BaoStock discovery")
    result = sync_date(project=tmp_path, config_path="config.json", target=date(2026, 9, 11),
                       now=datetime(2026, 9, 11, 23, tzinfo=SHANGHAI), client=NoBaoStockLists(),
                       calendar_resolver=lambda **k: {"status": "verified", "calendar_verified": True,
                           "resolved_trade_date": "2026-09-11", "cached": True, "response": {"status": "permission_denied"}},
                       exchange_transport=transport, offline_test=True)
    assert len(calls) == 4 and result["implementation_stage"] == "F1.1"
    assert result["ordinary_a_count"] == 4 and result["universe_verified"] is False
    assert result["verification_kind"] == "offline_test"
    assert result["board_acceptance"]["bse"]["ordinary_a_count"] is None
    assert "permission_unconfirmed:bse:listed_shares" not in result["blockers"]
    frozen = json.loads(Path(result["snapshot_path"]).read_text(encoding="utf-8"))
    assert len(frozen["members"]) == 4 and frozen["unknown_status_count"] == 4
    assert all(m["name"].startswith("OFFLINE_TEST") for m in frozen["members"])
