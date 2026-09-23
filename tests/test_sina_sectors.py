"""Contract and failure evidence for bounded sector discovery, without networking."""
from copy import deepcopy
from datetime import datetime, timedelta
import base64
import hashlib
import json
from pathlib import Path
import time

import pytest

from ashare_daily.providers.base import SecurityIdentity
from ashare_daily.providers.sina_sectors import (
    CATALOG_URL, QUOTE_BASE, SHANGHAI, SinaHttpClient, SinaSectorProvider,
    allowed_url, count_url, members_url, parse_catalog, parse_members, parse_quotes,
)


PERMISSION = {"enabled": True, "user_authorized": True, "permission_status": "approved", "permission_basis": "test fixture only",
    "purpose": "personal_noncommercial_local_research", "permitted_storage": True,
    "permitted_automated_access": True, "llm_export": False, "upstream_grant_status": "unconfirmed"}
SECTOR = {"sector_id": "sina:new_demo", "provider_id": "new_demo", "taxonomy": "sina_industry", "displayed_count": 2}


def response(url, text, *, status=200, kind="offline_test"):
    raw = text.encode("gb18030")
    return {"url": url, "ok": status == 200, "http_status": status, "status": "received" if status == 200 else "permission_denied" if status in {401, 403} else "rate_limited",
        "body_complete": True, "body_base64": base64.b64encode(raw).decode(), "body_sha256": hashlib.sha256(raw).hexdigest(),
        "body_bytes": len(raw), "fetched_at": "2026-09-11T18:00:00+08:00", "mode": "offline_test",
        "verification_kind": kind, "headers": {"content-type": "text/plain"}}


def directory_row(label="new_demo", count=2, name="样例行业"):
    return ",".join([label, name, str(count), "10", "0.1", "1", "100000", "1000000", "sh600001", "1", "10", "0.1", "示例"])


def directory(rows=None):
    return "var S_Finance_bankuai_sinaindustry = " + json.dumps(rows or {"new_demo": directory_row()}, ensure_ascii=False)


def identity(code="600001", exchange="SSE", board="sse_main"):
    return SecurityIdentity("test-" + exchange + code, code, exchange, board, metadata_verified=True)


def quote(symbol="sh600001", *, day="2026-09-11", clock="15:34:59", price="11", reference="10", amount="1234.567", volume="1234"):
    fields = ["样例", "10", reference, price, "12", "9", "0", "0", volume, amount] + ["0"] * 20 + [day, clock, "00", ""]
    return f'var hq_str_{symbol}="' + ",".join(fields) + '";\n'


def member(code):
    return {"symbol": "sh" + code, "code": code, "name": "样例", "ticktime": "15:00:00"}


def provider(tmp_path, data, *, permission=PERMISSION):
    calls = []
    def transport(url):
        calls.append(url)
        value = data[url]
        if isinstance(value, list):
            value = value.pop(0)
        return response(url, value)
    return SinaSectorProvider(tmp_path / "offline", mode="offline_test", transport=transport,
        permission=permission, pause_seconds=0), calls


def test_catalog_full_document_boundary_keeps_undated_amount_unverified():
    rows = parse_catalog(response(CATALOG_URL, directory()))
    assert len(rows) == 1
    assert rows[0]["source_business_date"] is None
    assert rows[0]["date_verified"] is False
    assert rows[0]["source_amount_cny"] is None
    assert rows[0]["source_amount_raw"] == "1000000"
    assert rows[0]["source_change_pct"] == "1"


def test_catalog_new_stock_basket_is_not_industry():
    rows = parse_catalog(response(CATALOG_URL, directory({"new_stock": directory_row("new_stock", name="次新股")})))
    assert rows[0]["kind"] == "other"


@pytest.mark.parametrize("text", [directory()[:-1], directory()+";run()", "callback("+directory()+")", "{}",
    'var S_Finance_bankuai_sinaindustry={"x":"x","x":"x"}',
    'var S_Finance_bankuai_sinaindustry={"new_demo":"wrong,label"}'])
def test_catalog_rejects_truncation_untrusted_code_and_duplicate_keys(text):
    with pytest.raises(ValueError):
        parse_catalog(response(CATALOG_URL, text))


@pytest.mark.parametrize("change", [{"body_sha256": "0"*64}, {"body_complete": False}, {"body_bytes": 1},
    {"http_status": 403}, {"mode": "research"}, {"fetched_at": "2026-09-11T18:00:00"}])
def test_catalog_rejects_incomplete_or_forged_http_provenance(change):
    value = response(CATALOG_URL, directory())
    value.update(change)
    with pytest.raises(ValueError):
        parse_catalog(value)


def test_catalog_rejects_future_observation():
    value = response(CATALOG_URL, directory())
    value["fetched_at"] = (datetime.now(SHANGHAI) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="observation_time"):
        parse_catalog(value)


def test_complete_members_continue_past_short_page_and_stale_display_count(tmp_path):
    source, calls = provider(tmp_path, {count_url("new_demo"): ['"3"', '"3"'],
        members_url("new_demo", 1): json.dumps([member("600001"), member("600002")]),
        members_url("new_demo", 2): json.dumps([member("600003")]), members_url("new_demo", 3): "[]"})
    result = source.fetch_members(SECTOR)
    assert result["complete"] and result["boundary_verified"]
    assert len(result["rows"]) == 3 and len(calls) == 5
    assert result["count_discrepancy"]["catalog_differs"] is True
    assert result["pages"][-1]["explicit_terminal_empty_list"] is True
    assert result["source_business_date"] is None
    assert result["rows"][0]["security_type"] is None


def test_more_than_100_members_are_not_truncated(tmp_path):
    values = [member(str(600000+i)) for i in range(121)]
    source, calls = provider(tmp_path, {count_url("new_demo"): ['"121"', '"121"'],
        members_url("new_demo", 1): json.dumps(values[:80]), members_url("new_demo", 2): json.dumps(values[80:]),
        members_url("new_demo", 3): "[]"})
    result = source.fetch_members(SECTOR)
    assert result["complete"] and len(result["rows"]) == 121
    assert calls[-1] == count_url("new_demo")


@pytest.mark.parametrize("last", ["", "null", "{}", '[{"symbol":"sh600099"}'])
def test_full_page_network_empty_or_malformed_is_not_terminal(tmp_path, last):
    values = [member(str(600000+i)) for i in range(80)]
    source, _ = provider(tmp_path, {count_url("new_demo"): '"80"',
        members_url("new_demo", 1): json.dumps(values), members_url("new_demo", 2): last})
    result = source.fetch_members(SECTOR)
    assert not result["complete"] and not result["boundary_verified"]
    assert len(result["rows"]) == 80


def test_count_mismatch_retains_all_rows_and_blocks(tmp_path):
    source, _ = provider(tmp_path, {count_url("new_demo"): ['"1"','"1"'],
        members_url("new_demo", 1): json.dumps([member("600001"),member("600002")]),
        members_url("new_demo", 2): "[]"})
    result = source.fetch_members(SECTOR)
    assert result["boundary_verified"] and not result["complete"]
    assert len(result["rows"]) == 2
    assert "membership_count_endpoint_discrepancy_unresolved" in result["issues"]


def test_count_changing_between_queries_blocks(tmp_path):
    source, _ = provider(tmp_path, {count_url("new_demo"): ['"2"','"3"'],
        members_url("new_demo", 1): json.dumps([member("600001"),member("600002")]),
        members_url("new_demo", 2): "[]"})
    assert "membership_changed_during_collection" in source.fetch_members(SECTOR)["issues"]


def test_duplicate_page_is_retained_and_stopped(tmp_path):
    values = json.dumps([member("600001"),member("600002")])
    source, calls = provider(tmp_path, {count_url("new_demo"): '"4"',
        members_url("new_demo", 1): values, members_url("new_demo", 2): values})
    result = source.fetch_members(SECTOR)
    assert not result["complete"] and len(result["rows"]) == 4
    assert len(calls) == 3
    assert "duplicate_member_across_pages" in result["issues"]


def test_unknown_member_type_and_code_conflicts_are_preserved():
    values = [member("600001"), {"symbol":"sh600002","code":"600003","name":"冲突"},
        {"symbol":"opaque","code":"123","name":"未知"}]
    rows = parse_members(response(members_url("new_demo",1),json.dumps(values)))
    assert len(rows) == 3
    assert rows[0]["metadata_verified"] is False and rows[0]["listing_board"] is None
    assert "membership_symbol_code_conflict" in rows[1]["issues"]
    assert rows[2]["exchange"] is None


def test_quotes_all_four_boards_keep_dated_numerical_facts_and_unknown_status():
    ids = [identity(),identity("000001","SZSE","szse_main"),identity("300001","SZSE","chinext"),identity("688001","SSE","star")]
    symbols = ["sh600001","sz000001","sz300001","sh688001"]
    rows, missing = parse_quotes(response(QUOTE_BASE+",".join(symbols),"".join(quote(s) for s in symbols)),ids,"2026-09-11")
    assert not missing and len(rows) == 4
    for row in rows:
        assert row["date_verified"] and row["after_close"] and not row["issues"]
        assert row["change_pct"] == "10.0" and row["volume_shares"] == 1234
        assert row["amount_cny"] == "1234.567"
        assert row["is_st"] is None and row["tradestatus"] is None
        assert row["status_complete"] is False and row["is_closing_daily_bar"] is False


@pytest.mark.parametrize("options,issue", [({"day":"2026-09-10"},"quote_date_mismatch"),
    ({"clock":"14:59:59"},"quote_before_post_close_boundary"),
    ({"clock":"18:00:01"},"quote_timestamp_after_observation"),
    ({"day":""},"quote_source_date_or_time_invalid"),({"price":"0"},"quote_price_numeric_field_out_of_range"),
    ({"reference":"0"},"quote_price_numeric_field_out_of_range"),
    ({"volume":"12.5"},"quote_volume_or_amount_volume_not_integral_shares"),
    ({"amount":"NaN"},"quote_volume_or_amount_numeric_field_out_of_range")])
def test_quote_quality_does_not_fill_missing_price_date_or_status(options, issue):
    rows,_ = parse_quotes(response(QUOTE_BASE+"sh600001",quote(**options)),[identity()],"2026-09-11")
    assert issue in rows[0]["issues"]
    assert rows[0]["tradestatus"] is None


def test_last_update_at_auction_close_requires_observation_after_postclose():
    body = response(QUOTE_BASE+"sh600001", quote(clock="15:00:00"))
    rows, _ = parse_quotes(body, [identity()], "2026-09-11")
    assert rows[0]["after_close"] and not rows[0]["issues"]
    assert rows[0]["normalization_version"] == "sina-dated-light-quote-v2"
    assert rows[0]["full_day_turnover_verified"] is False
    body["fetched_at"] = "2026-09-11T15:29:59+08:00"
    rows, _ = parse_quotes(body, [identity()], "2026-09-11")
    assert "quote_before_post_close_boundary" in rows[0]["issues"]


@pytest.mark.parametrize("text", [quote()+"process.exit();", quote()+quote(),quote("sh600002"),
    'var hq_str_sh600001="bad\\escape";', 'callback('+quote()+')'])
def test_quote_boundary_and_identity_conflicts_rejected(text):
    with pytest.raises(ValueError):
        parse_quotes(response(QUOTE_BASE+"sh600001",text),[identity()],"2026-09-11")


def test_quote_missing_requested_symbol_is_explicit():
    rows, missing = parse_quotes(response(QUOTE_BASE+"sh600001,sh600002",quote()),[identity(),identity("600002")],"2026-09-11")
    assert len(rows) == 1 and missing == ["sh600002"]


def test_quote_no_source_date_from_requested_future_date():
    with pytest.raises(ValueError,match="target_after"):
        parse_quotes(response(QUOTE_BASE+"sh600001",quote()),[identity()],"2026-09-12")


def test_quote_request_batches_without_offset_source_parameter(tmp_path):
    ids = [identity(str(600000+i)) for i in range(81)]
    first = ["sh"+item.code for item in ids[:80]]
    last = "sh"+ids[-1].code
    source,calls = provider(tmp_path,{QUOTE_BASE+",".join(first):"".join(quote(s) for s in first),QUOTE_BASE+last:quote(last)})
    result=source.fetch_quotes(ids,"2026-09-11")
    assert result["complete"] and result["quote_complete"] and len(result["rows"])==81
    assert len(calls)==2 and all("offset" not in url for url in calls)
    assert result["research_ready"] is False and result["status_complete"] is False


def test_quote_excludes_unverified_or_nonordinary_identity_before_transport(tmp_path):
    source,calls=provider(tmp_path,{})
    raw={"security_id":"test","code":"600001","exchange":"SSE","board":"sse_main","metadata_verified":False}
    with pytest.raises(ValueError): source.fetch_quotes([raw],"2026-09-11")
    raw.update(metadata_verified=True,security_type="cdr")
    with pytest.raises(ValueError): source.fetch_quotes([raw],"2026-09-11")
    assert not calls


def test_unconfirmed_permission_makes_zero_requests(tmp_path):
    source,calls=provider(tmp_path,{},permission={})
    result=source.fetch_catalog()
    assert not calls and not result["complete"]
    assert result["evidence"]==[] and "source_permission_required" in result["issues"]


@pytest.mark.parametrize("key,value",[("enabled",False),("user_authorized",False),
    ("permission_status","unconfirmed"),("permission_basis"," "),("permitted_storage",False),
    ("permitted_automated_access",False),("llm_export",True),("purpose","commercial_redistribution")])
def test_revoked_or_missing_permission_stops_even_direct_client_access(tmp_path,key,value):
    permission=deepcopy(PERMISSION)
    permission[key]=value
    source,calls=provider(tmp_path,{},permission=permission)
    result=source.client.get(CATALOG_URL)
    assert not calls and result["status"]=="permission_required"
    assert result["verification_kind"]=="not_requested"
    assert result["metrics"]["requests"]==0
    del permission[key]
    source2,calls2=provider(tmp_path/"missing",{},permission=permission)
    assert source2.client.get(CATALOG_URL)["status"]=="permission_required"
    assert not calls2


def test_permission_preserves_unconfirmed_upstream_grant(tmp_path):
    source,_=provider(tmp_path,{CATALOG_URL:directory()})
    result=source.fetch_catalog()
    assert result["complete"] and result["upstream_grant_status"]=="unconfirmed"
    assert result["provenance_mode"]=="offline_test"
    evidence=json.loads(Path(result["evidence"][0]["path"]).read_text(encoding="utf-8"))
    assert evidence["verification_kind"]=="offline_test"
    assert evidence["metrics"]["network_requests"]==0
    assert hashlib.sha256(Path(evidence["body_path"]).read_bytes()).hexdigest()==evidence["body_sha256"]


@pytest.mark.parametrize("status",[401,403,429])
def test_access_denial_stops_source_without_new_header_or_retry(tmp_path,status):
    calls=[]
    def transport(url):
        calls.append(url)
        return response(url,"",status=status)
    client=SinaHttpClient(tmp_path,mode="offline_test",permission=PERMISSION,transport=transport,pause_seconds=0)
    first=client.get(CATALOG_URL)
    second=client.get(count_url("new_demo"))
    assert len(calls)==1 and first["http_status"]==status
    assert second["status"]=="circuit_open" and second["verification_kind"]=="not_requested"


def test_repeated_network_failure_ends_route(tmp_path):
    calls=[]
    def transport(url):
        calls.append(url)
        value=response(url,"")
        value.update(ok=False,http_status=None,status="timeout",body_complete=False)
        return value
    client=SinaHttpClient(tmp_path,mode="offline_test",permission=PERMISSION,transport=transport,pause_seconds=0)
    client.get(CATALOG_URL)
    client.get(CATALOG_URL)
    result=client.get(CATALOG_URL)
    assert len(calls)==2 and result["status"]=="circuit_open"


def test_overall_deadline_ends_quote_batches_without_shrinking_expected_count(tmp_path):
    ids = [identity(str(600000+i)) for i in range(81)]
    source,calls = provider(tmp_path,{})
    source.client.deadline_monotonic = time.monotonic()-1
    result = source.fetch_quotes(ids,"2026-09-11")
    assert not calls and result["expected_count"]==81
    assert len(result["missing_symbols"])==81 and not result["complete"]
    assert source.blocked and source.client.stopped=="runtime_limit_exhausted"


def test_source_interval_cannot_exceed_overall_deadline(tmp_path):
    source,calls=provider(tmp_path,{CATALOG_URL:directory()})
    source.client.pause_seconds=2
    source.client._last=time.monotonic()
    source.client.deadline_monotonic=time.monotonic()+.1
    result=source.fetch_catalog()
    assert not calls and not result["complete"]
    assert source.client.stopped=="runtime_limit_exhausted"


def test_worker_timeout_is_capped_by_remaining_overall_budget(tmp_path,monkeypatch):
    # Exercise the parent worker protocol; no subprocess/socket is created.
    import queue
    import io
    client=SinaHttpClient(tmp_path,permission=PERMISSION,timeout_seconds=15,pause_seconds=0)
    class Process:
        stdin=io.StringIO()
        def poll(self): return None
    fake=Process()
    client._process=fake
    client._events=queue.Queue()
    value=response(CATALOG_URL,directory(),kind="live_network")
    client._events.put(json.dumps({"id":0,"result":value}))
    client.deadline_monotonic=time.monotonic()+.25
    result=client._live(CATALOG_URL)
    request=json.loads(fake.stdin.getvalue())
    assert 0 < request["timeout"] <= .25 and result["ok"]
    client._process=None


def test_test_live_modes_cannot_be_substituted(tmp_path):
    with pytest.raises(ValueError): SinaHttpClient(tmp_path,mode="research",transport=lambda url:{})
    with pytest.raises(ValueError): SinaHttpClient(tmp_path,mode="offline_test")
    client=SinaHttpClient(tmp_path,mode="offline_test",transport=lambda url:response(url,directory(),kind="live_network"),permission=PERMISSION)
    with pytest.raises(ValueError,match="offline_test"): client.get(CATALOG_URL)


@pytest.mark.parametrize("url",["http://hq.sinajs.cn/list=sh600001","https://hq.sinajs.cn/list=sh600001&callback=x",
    "https://hq.sinajs.cn/list=sh600001,sh600001","https://hq.sinajs.cn/list=bj920001",
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount?node=x&offset=1",
    "https://user:pass@finance.sina.com.cn/realstock/company/sh600001/qfq.js",
    "https://finance.sina.com.cn/realstock/company/sh600001/qfq.js#fragment"])
def test_access_allowlist_does_not_invent_api_or_expand_board_scope(url):
    with pytest.raises(ValueError): allowed_url(url)


def test_history_agent_uses_same_bounded_allowlist():
    for url in ["https://finance.sina.com.cn/realstock/company/sh600001/hisdata_klc2/klc_kl.js",
        "https://finance.sina.com.cn/realstock/company/sh600001/qfq.js",
        "https://quotes.sina.cn/cn/api/jsonp.php/var%20_sh6880012026_09_11=/KC_MarketDataService.getKLineData?symbol=sh688001"]:
        assert allowed_url(url)==url


def test_long_evidence_path_preserves_logical_locator_and_raw_hash(tmp_path):
    from ashare_daily.operations.backup import _io
    directory_path = tmp_path / ("a"*90) / ("b"*90) / ("c"*90)
    source,_=provider(directory_path,{CATALOG_URL:directory()})
    result=source.fetch_catalog()
    assert result["complete"]
    record_path=Path(result["evidence"][0]["path"])
    assert not str(record_path).startswith("\\\\?\\")
    record=json.loads(_io(record_path).read_text(encoding="utf-8"))
    assert hashlib.sha256(_io(Path(record["body_path"])).read_bytes()).hexdigest()==record["body_sha256"]


@pytest.mark.parametrize("kwargs",[{"timeout_seconds":True},{"timeout_seconds":float("nan")},
    {"pause_seconds":-1},{"max_requests":2001},{"max_requests":True}])
def test_runtime_bounds_cannot_expand_silently(tmp_path,kwargs):
    with pytest.raises(ValueError): SinaHttpClient(tmp_path,mode="offline_test",transport=lambda url:{},**kwargs)
