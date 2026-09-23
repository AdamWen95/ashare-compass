"""Synthetic offline status evidence only; no SDK, network or research DB."""
from copy import deepcopy
import hashlib
import json

import pytest

from ashare_daily.calendar import _digest
from ashare_daily.market_foundation import _hash, normalize_baostock_rows
from ashare_daily.providers import baostock as bs
from ashare_daily.providers import sector_status as status

DAY = "2026-09-10"
STAMP = "2026-09-11T18:00:00+08:00"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, ensure_ascii=False).encode()
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def sdk(symbol, *, flag="0"):
    params = dict(code=symbol, start_date=DAY, end_date=DAY, security_type="stock", adjustment_mode="unadjusted")
    row = dict(date=DAY, code=symbol, open="10", high="10", low="10", close="10", preclose="10",
               volume="0", amount="0", adjustflag="3", tradestatus=flag, isST="")
    response = bs.base_result("history_f2", params)
    response.update(ok=True, status="ok", error_code="0", fields=bs.HISTORY_STOCK_FIELDS, rows=[row],
        fetched_at=STAMP, raw_hash=bs.raw_hash(bs.HISTORY_STOCK_FIELDS, [row]),
        login=dict(ok=True, error_code="0", error_msg="success"),
        provenance_mode="offline_test", verification_kind="offline_test",
        diagnostics={"failure_stage": None, "hard_timeout": False, "events": [
            dict(stage="query_wait", state="completed", error_code="0", initial_page="1", initial_page_records=1),
            dict(stage="row_read", state="completed", row_count=1)]})
    return response


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path
    write(root / "config/sse_szse_market_providers.json", dict(schema_version="f2-market-config-v1",
        scope="sse_szse_a", provider="baostock", permission_status="approved", permission_basis="offline fixture", model_calls=0))
    write(root / "config/sse_szse_universe.json", {"sources": [dict(provider="baostock", enabled=True,
        permission_status="approved", permission_basis="offline fixture", llm_export=False)]})
    identities = {}
    for index in (1, 2):
        identities[f"sz.00000{index}"] = dict(security_id=f"synthetic-status-{index}", code=f"00000{index}",
            exchange="SZSE", board="szse_main", security_type="ordinary_a", metadata_verified=True)
    universe = dict(mode="offline_test", scope="sse_szse_a", requested_date=DAY, resolved_trade_date=DAY,
        universe_verified=True, calendar_verified=True, collection_ready=True, snapshot_id="synthetic-offline-universe",
        members=[{**deepcopy(i), "provenance_mode": "offline_test", "statuses": {"suspended": {"value": None}}} for i in identities.values()])
    quotes = dict(provenance_mode="offline_test", rows=[dict(symbol=s.replace(".", ""), security_id=i["security_id"],
        identity=deepcopy(i), trade_date=DAY, tradestatus=None, is_st=None, close=None, issues=["zero_price"],
        status_issues=["trading_status_unknown", "st_status_unknown"]) for s, i in identities.items()])
    folder = root / "offline_test/status"
    params = {"start_date": DAY, "end_date": DAY}
    calresponse = bs.base_result("calendar", params)
    rows = [dict(calendar_date=DAY, is_trading_day="1")]
    calresponse.update(ok=True, status="ok", error_code="0", fields=bs.CALENDAR_FIELDS, rows=rows,
        fetched_at=STAMP, raw_hash=bs.raw_hash(bs.CALENDAR_FIELDS, rows),
        login=dict(ok=True, error_code="0", error_msg="success"))
    calendar = dict(schema_version="f1-calendar-cache-v1", provider="baostock", mode="offline_test", response=calresponse)
    calendar["content_hash"] = _digest(calendar)
    calpath = folder / "calendar.json"
    calhash = write(calpath, calendar)
    manifest = dict(schema_version=status.SCHEMA, target_date=DAY, scope="sse_szse_a", mode="offline_test",
        online_requested=False, source_endpoint=status.ENDPOINT, permission_basis="offline fixture",
        universe_snapshot_id=universe["snapshot_id"], observed_at=STAMP, identities=identities,
        calendar=dict(source_path=str(calpath), source_file_hash=calhash, source_raw_hash=calresponse["raw_hash"]))
    result = dict(schema_version=status.SCHEMA, target_date=DAY, cached={}, online={})
    for n, (symbol, identity) in enumerate(identities.items()):
        response = sdk(symbol)
        path = folder / f"response-{n}.json"
        ref = dict(source_file_hash=write(path, response))
        if n == 0:
            quality = normalize_baostock_rows(response["rows"], security_id=identity["security_id"], symbol=symbol,
                start_date=DAY, end_date=DAY, trading_dates=(DAY,))
            ref.update(source_path=str(path), source_raw_hash=response["raw_hash"], fact_hash=_hash(quality["records"][0]))
            result["cached"][symbol] = dict(evidence=[ref])
        else:
            ref.update(path=str(path), raw_hash=response["raw_hash"], full_day_halt_evidence=False)
            result["online"][symbol] = ref
    write(folder / "manifest.json", manifest)
    write(folder / "result.json", result)
    return root, universe, quotes, folder, manifest, result


def apply(fixture):
    root, universe, quotes, folder, _, _ = fixture
    return status.apply_status_evidence(root, universe, quotes, DAY, folder / "result.json")


def update_response(fixture, mutate):
    _, _, _, folder, _, result = fixture
    ref = result["online"]["sz.000002"]
    path = folder / "response-1.json"
    response = json.loads(path.read_text())
    mutate(response)
    response["raw_hash"] = bs.raw_hash(response["fields"], response["rows"])
    ref["raw_hash"] = response["raw_hash"]
    ref["source_file_hash"] = write(path, response)
    write(folder / "result.json", result)


def test_actual_source_rows_overlay_without_mutating_facts_unknowns_or_universe(fixture, monkeypatch):
    root, universe, quotes, folder, _, _ = fixture
    original = deepcopy((universe, quotes))
    monkeypatch.setattr(bs.BaoStockClient, "query", lambda *a, **k: pytest.fail("network attempted"))
    result = apply(fixture)
    assert (universe, quotes) == original
    assert len(result["file_refs"]) == 5  # result, manifest, calendar and two raw responses.
    for row in result["rows"]:
        evidence = row["full_day_halt_evidence"]
        assert evidence["value"] is evidence["verified"] is evidence["full_day"] is True
        assert evidence["as_of_date"] == evidence["effective_from"] == evidence["effective_to"] == DAY
        assert evidence["observed_at"] == STAMP and evidence["historical_reconstruction"] is True
        assert row["tradestatus"] is row["is_st"] is row["close"] is None
        assert row["status_issues"] == ["trading_status_unknown", "st_status_unknown"]
    assert status.apply_status_evidence(root, universe, result, DAY, folder / "result.json") == result
    assert not list(root.rglob("*.sqlite3"))


@pytest.mark.parametrize("mutation", [
    lambda r: r["parameters"].update(code="sz.000003"),
    lambda r: r["parameters"].update(end_date="2026-09-11"),
    lambda r: r["parameters"].update(adjustment_mode="forward_adjusted"),
    lambda r: r["rows"][0].update(date="2026-09-09"),
    lambda r: r["rows"][0].update(code="sz.000003"),
    lambda r: r["rows"].append(deepcopy(r["rows"][0])),
    lambda r: r["rows"][0].update(tradestatus=""),
    lambda r: r["rows"][0].update(adjustflag="2"),
    lambda r: r["login"].update(ok=False),
    lambda r: r.update(fetched_at="2099-01-01T21:00:00+08:00"),
    lambda r: r["diagnostics"].update(events=[]),
    lambda r: r["diagnostics"]["events"][0].update(initial_page_records=2),
    lambda r: r["diagnostics"]["events"][1].update(row_count=0),
    lambda r: r.update(provenance_mode="online"),
])
def test_bad_date_identity_login_boundary_or_provenance_fails_even_with_updated_hash(fixture, mutation):
    update_response(fixture, mutation)
    with pytest.raises(ValueError):
        apply(fixture)


def test_summary_boolean_does_not_override_trading_source_row(fixture):
    update_response(fixture, lambda r: r["rows"][0].update(tradestatus="1"))
    result = apply(fixture)
    assert "full_day_halt_evidence" not in result["rows"][1]
    assert result["rows"][1]["tradestatus"] is None


def test_cached_fact_hash_is_recomputed_from_original_source(fixture):
    _, _, _, folder, _, result = fixture
    result["cached"]["sz.000001"]["evidence"][0]["fact_hash"] = "0" * 64
    write(folder / "result.json", result)
    with pytest.raises(ValueError, match="cached_fact_hash"):
        apply(fixture)


def test_source_body_edit_without_hash_update_rejected(fixture):
    folder = fixture[3]
    (folder / "response-1.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="file_hash"):
        apply(fixture)


def test_permission_revocation_stops_before_loading_evidence(fixture):
    root = fixture[0]
    write(root / "config/sse_szse_market_providers.json", {"permission_status": "revoked"})
    with pytest.raises(ValueError, match="permission_required"):
        apply(fixture)


@pytest.mark.parametrize("mutation", [
    lambda u, q, m: m.update(target_date="2026-09-09"),
    lambda u, q, m: m.update(universe_snapshot_id="another-universe"),
    lambda u, q, m: m["identities"]["sz.000002"].update(security_id="wrong-identity"),
    lambda u, q, m: q["rows"][1].update(trade_date="2026-09-09"),
    lambda u, q, m: q["rows"][1].update(tradestatus=True),
    lambda u, q, m: q["rows"][1]["identity"].update(board="star"),
    lambda u, q, m: m["calendar"].pop("source_file_hash"),
    lambda u, q, m: q["rows"].append(deepcopy(q["rows"][1])),
    lambda u, q, m: u.update(mode="research"),
])
def test_manifest_quote_identity_conflicts_and_test_isolation_rejected(fixture, mutation):
    _, universe, quotes, folder, manifest, _ = fixture
    mutation(universe, quotes, manifest)
    write(folder / "manifest.json", manifest)
    with pytest.raises(ValueError):
        apply(fixture)


def test_conflicting_cached_and_new_source_status_is_not_silently_resolved(fixture):
    _, _, _, folder, _, result = fixture
    symbol = "sz.000001"
    response = sdk(symbol, flag="1")
    path = folder / "conflict.json"
    result["online"][symbol] = dict(path=str(path), source_file_hash=write(path, response), raw_hash=response["raw_hash"])
    write(folder / "result.json", result)
    with pytest.raises(ValueError, match="sources_conflict"):
        apply(fixture)
