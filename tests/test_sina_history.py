"""Synthetic Sina fixtures are confined to offline_test temporary paths."""
import base64
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json

import pytest

from ashare_daily.market_foundation import F2MarketStore
from ashare_daily.providers.base import DailyBarRequest, SecurityIdentity
from ashare_daily.providers import sina_history as sina

DAY = "2026-09-10"
STAMP = "2026-09-11T21:00:00+08:00"


def identity(board="star", code=None, sid="offline-sina-id"):
    exchange = "SSE" if board in {"star", "sse_main"} else "SZSE"
    code = code or {"star": "688001", "sse_main": "600001", "szse_main": "000001", "chinext": "300001"}[board]
    return SecurityIdentity(sid, code, exchange, board, metadata_verified=True)


def request(board="star", adjustment="unadjusted", days=(DAY,), **kwargs):
    return DailyBarRequest(identity(board, **kwargs), days[0], days[-1], days, adjustment)


def http(url, body, *, stamp=STAMP, mode="offline_test"):
    raw = body.encode()
    return {"url": url, "status": "received", "ok": True, "http_status": 200, "body_complete": True,
        "body_base64": base64.b64encode(raw).decode(), "body_bytes": len(raw), "body_sha256": hashlib.sha256(raw).hexdigest(),
        "fetched_at": stamp, "verification_kind": "offline_test" if mode == "offline_test" else "live_network",
        "provenance_mode": "offline_test" if mode == "offline_test" else "online", "metrics": {"requests": 1, "retries": 0}}


def raw_rows(days=(DAY,), **updates):
    return [{"d": day, "o": "10", "h": "11", "l": "9", "c": "10.5", "v": "1000", "pv": "12", "pa": "120", **updates} for day in days]


def response(req=None, *, rows=None, factor="2", factor_rows=None, stamp=STAMP):
    req = req or request()
    symbol = sina.source_symbol(req.identity)
    observed = stamp[:10]
    body = "var _" + symbol + observed.replace("-", "_") + "=(" + json.dumps(raw_rows(req.expected_dates) if rows is None else rows) + ");"
    if req.identity.board != "star":
        body = 'var KLC_K2_' + symbol + '="ABC";'
    raw = http(sina.history_url(req.identity, observed), body, stamp=stamp)
    factors = None
    if req.adjustment_mode == "forward_adjusted":
        records = factor_rows if factor_rows is not None else [{"d": "1900-01-01", "f": factor}]
        factors = http(sina.factor_url(req.identity), "var " + symbol + "qfq=" + json.dumps({"total": len(records), "data": records}) + ";", stamp=stamp)
    return {"schema_version": sina.SCHEMA, "provider": "sina", "operation": "daily_bars", "identity": asdict(req.identity),
        "parameters": req.parameters(), "expected_dates": list(req.expected_dates), "source_symbol": symbol,
        "source_endpoint": raw["url"], "source_business_date": None, "ok": True, "error_code": "0", "status": "partial",
        "provenance_mode": "offline_test", "verification_kind": "offline_test", "fetched_at": stamp,
        "raw_history": raw, "factor_table": factors, "raw_hash": sina.response_hash(raw, factors)}


def test_star_missing_amount_unknown_states_do_not_become_normal():
    result = sina.normalize_sina_response(response(), request(), mode="offline_test")
    row = result["records"][0]
    assert row["volume_shares"] == 1000 and row["amount_cny"] is None
    assert row["tradestatus"] is None and row["is_st"] is None
    assert row["preclose"] is None and "missing_amount_cny" in row["quality_flags"]
    assert result["numeric_price_complete"] and not result["numeric_history_complete"]
    assert result["quote_complete"] is False and result["research_ready"] is False


@pytest.mark.parametrize("board", ["sse_main", "szse_main", "chinext"])
def test_four_board_metadata_and_whole_history_metrics(monkeypatch, board):
    req = request(board)
    monkeypatch.setattr(sina, "decode_pinned", lambda encoded: [
        {"date": "2020-01-02", "open": 2, "high": 3, "low": 1, "close": 2, "volume": 20, "amount": 40},
        {"date": DAY, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000, "amount": 10000}])
    result = sina.normalize_sina_response(response(req), req, mode="offline_test")
    assert result["raw_row_count"] == 2 and len(result["records"]) == 1
    assert result["numeric_history_complete"] and not result["quote_complete"]
    assert result["physical_response_scope"] == "single_security_available_whole_history"
    assert result["source_business_date"] is None


def test_factor_nonunit_calculation_complete_anchor_and_no_fake_risk():
    req = request(adjustment="forward_adjusted")
    result = sina.normalize_sina_response(response(req), req, mode="offline_test")
    assert result["records"][0]["close"] == "5.25"
    assert result["records"][0]["adjustment_factor"] == "2"
    assert result["adjustment_window_complete"] is True
    assert result["quote_complete"] is False and result["status_complete"] is False


@pytest.mark.parametrize("text", [
    'var expected=[]; process.exit(0)', 'var other=[];', 'var expected={"x":1,"x":2};',
    'var expected={"x":NaN};', 'var expected=[function(){return 1}];', 'var expected=([]);',
])
def test_remote_executable_or_ambiguous_json_is_rejected(text):
    with pytest.raises(ValueError):
        sina.parse_assignment(text, "expected")


def test_comments_are_inert_and_callback_end_required():
    assert sina.parse_assignment('/* <script>location.href="bad"</script> */ var expected=([]);', "expected", parenthesized=True) == []
    with pytest.raises(ValueError):
        sina.parse_assignment('var expected=([];', "expected", parenthesized=True)


@pytest.mark.parametrize("updates", [{"o": "0"}, {"h": "8"}, {"v": "-2"}, {"v": "0.5"}, {"c": "NaN"}, {"o": "Infinity"}])
def test_invalid_prices_units_are_quarantined_without_dropping_expected(updates):
    quality = sina.normalize_sina_response(response(rows=raw_rows(**updates)), request(), mode="offline_test")
    assert quality["records"] == [] and quality["missing_dates"] == [DAY]
    assert not quality["numeric_price_complete"]


@pytest.mark.parametrize("kind", ["hash", "identity", "future", "mode", "date", "duplicates"])
def test_source_binding_provenance_and_dates_fail_closed(kind):
    req = request()
    value = response()
    if kind == "hash":
        value["raw_history"]["body_sha256"] = "0" * 64
    elif kind == "identity":
        value["identity"]["code"] = "688002"
    elif kind == "future":
        value["fetched_at"] = (datetime.now(sina.SHANGHAI) + timedelta(days=1)).isoformat()
    elif kind == "mode":
        with pytest.raises(ValueError):
            sina.normalize_sina_response(value, req, mode="research")
        return
    elif kind == "date":
        value = response(rows=raw_rows(("2026-09-09",)))
    else:
        value = response(rows=raw_rows() * 2)
    if kind in {"date", "duplicates"}:
        assert not sina.normalize_sina_response(value, req, mode="offline_test")["calendar_coverage_complete"]
    else:
        with pytest.raises(ValueError):
            sina.normalize_sina_response(value, req, mode="offline_test")


def test_factors_require_total_boundary_and_date_coverage():
    req = request(adjustment="forward_adjusted")
    value = response(req, factor_rows=[{"d": "2026-09-11", "f": "2"}])
    quality = sina.normalize_sina_response(value, req, mode="offline_test")
    assert quality["factor_missing_dates"] == [DAY] and not quality["adjustment_window_complete"]
    raw = json.loads(base64.b64decode(value["factor_table"]["body_base64"]).decode().split("=", 1)[1].rstrip(";"))
    raw["total"] = 99
    value["factor_table"] = http(sina.factor_url(req.identity), "var sh688001qfq=" + json.dumps(raw) + ";")
    value["raw_hash"] = sina.response_hash(value["raw_history"], value["factor_table"])
    with pytest.raises(ValueError, match="boundary"):
        sina.normalize_sina_response(value, req, mode="offline_test")


def test_version_store_preserves_sina_adjusted_numeric_artifact_without_old_gate_changes(tmp_path):
    store = F2MarketStore(tmp_path / "offline_test" / "market.sqlite3", mode="offline_test")
    windows = []
    for factor in ("2", "3"):
        req = request(adjustment="forward_adjusted")
        value = response(req, factor=factor)
        path = store.path.parent / (factor + ".json")
        body = json.dumps(value).encode()
        path.write_bytes(body)
        saved = store.save_batch(security_id=req.identity.security_id, symbol=req.identity.symbol, scope="sse_szse_a",
            universe_snapshot_id="offline-universe", response=value, source_response_path=path,
            source_response_hash=hashlib.sha256(body).hexdigest(), trading_dates=req.expected_dates,
            provenance_mode="offline_test", adjustment_mode=req.adjustment_mode, provider="sina", request=req)
        assert saved["window_id"] and not saved["quality"]["quote_complete"]
        windows.append(saved["window_id"])
    assert windows[0] != windows[1]
    first = store.get_adjustment_window(windows[0])
    assert first["records"][0]["close"] == "5.25" and not first["research_ready"]
    assert first["factor_component_hash"] != store.get_adjustment_window(windows[1])["factor_component_hash"]


def test_research_fake_injection_and_offline_research_path_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        sina.SinaHistoryProvider(tmp_path, permission={}, transport=lambda url: {})
    with pytest.raises(ValueError):
        sina.SinaHistoryProvider(tmp_path / "research", permission={}, mode="offline_test", transport=lambda url: {})


def test_provider_reuses_raw_for_qfq_and_does_not_retry(tmp_path):
    permission = {"enabled": True, "user_authorized": True, "purpose": "personal_noncommercial_local_research", "llm_export": False,
        "permission_status": "approved", "permission_basis": "explicit offline fixture permission",
        "permitted_storage": True, "permitted_automated_access": True, "upstream_grant_status": "unconfirmed"}
    calls = []
    observed = datetime.now(sina.SHANGHAI).isoformat()
    raw_req = request()
    qfq_req = request(adjustment="forward_adjusted")
    data = response(qfq_req, stamp=observed)
    def transport(url):
        calls.append(url)
        return deepcopy(data["factor_table"] if url.endswith("qfq.js") else data["raw_history"])
    provider = sina.SinaHistoryProvider(tmp_path / "offline_test", permission=permission, mode="offline_test", transport=transport, pause_seconds=0)
    try:
        raw = provider.fetch_daily_bars(raw_req)
        qfq = provider.fetch_daily_bars(qfq_req)
        assert raw_req.identity.security_id in provider._raw
        archived_raw = deepcopy(raw.response)
        provider.release_security(raw_req.identity.security_id)
        assert provider._raw == {} and raw.response == archived_raw
    finally:
        provider.close()
    assert provider._raw == {}
    assert len(calls) == 2 and raw.metrics["requests"] == qfq.metrics["requests"] == 1
    assert raw.has_facts and qfq.quality["adjustment_window_complete"] and not qfq.usable
