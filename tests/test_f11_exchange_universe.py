"""Synthetic contract tests; these are never live securities or market totals."""
from copy import deepcopy
from datetime import date
import json

import pytest

from ashare_daily.providers.exchange_universe import (
    ComponentContractError, SSE_ENDPOINT, SSE_METADATA_ENDPOINT, SZSE_ENDPOINT,
    WebsiteTransport, discover_exchange_lists, normalize_sse, normalize_szse,
    sse_page, szse_page,
)


DAY = date(2026, 9, 11)
STAMP = "2026-09-11T18:00:00+08:00"


def sse_raw(code="900001", stock_type="1"):
    # Deliberately unfamiliar prefixes: source fields, not digits, decide scope.
    return {"A_STOCK_CODE": code, "COMPANY_CODE": code, "STOCK_TYPE": stock_type,
            "LIST_BOARD": "1" if stock_type == "1" else "2", "SEC_NAME_CN": "OFFLINE_TEST",
            "LIST_DATE": "20200102", "DELIST_DATE": "-", "STATE_CODE": "2"}


def sse_body(rows, *, page=1, pages=1, total=None, size=25):
    return {"sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L", "actionErrors": [], "fieldErrors": {},
            "queryDate": "", "result": rows,
            "pageHelp": {"pageNo": page, "pageSize": size, "pageCount": pages,
                         "total": len(rows) if total is None else total, "data": deepcopy(rows)}}


def szse_raw(code="890001", board="主板"):
    return {"agdm": code, "bk": board, "agjc": "<a href='https://invalid.test/'><u>OFFLINE_TEST&amp;</u></a>",
            "agssrq": "2020-01-02"}


def szse_body(rows, *, page=1, pages=1, total=None, size=20, source_date="2026-09-11"):
    return [{"metadata": {"catalogid": "1110", "tabkey": "tab1", "name": "A股列表",
                          "subname": source_date, "pageno": page, "pagesize": size, "pagecount": pages,
                          "recordcount": len(rows) if total is None else total,
                          "cols": {"agdm": "A股代码", "bk": "板块"}}, "data": rows},
            {"metadata": {"tabkey": "tab2", "name": "B股列表"}, "data": [{"agdm": "NON_TARGET"}]}]


def response(body):
    return {"ok": True, "status": "ok", "fetched_at": STAMP, "body": body,
            "raw_sha256": "0" * 64, "provenance_mode": "offline_test"}


def approved(*exchanges):
    return {e: {"enabled": True, "permission_status": "approved", "access_status": "allowed"} for e in exchanges}


def test_sse_explicit_main_a_type_does_not_use_prefix():
    value = normalize_sse(sse_raw(), stock_type="1", evidence_id="OFFLINE_TEST")
    assert value["security_type"] == "ordinary_a"
    assert value["board"] == "sse_main"
    assert value["code"] == "900001"
    assert value["statuses"] == {}


def test_sse_star_share_class_requires_individual_evidence():
    row = sse_raw("900002", "8")
    unknown = normalize_sse(row, stock_type="8", evidence_id="OFFLINE_TEST")
    assert unknown["security_type"] == "unknown"
    assert not unknown["metadata_verified"]
    detail = {"COMPANY_CODE": "900002", "A_STOCK_CODE": "900002", "SEC_TYPE": "科创CDR"}
    cdr = normalize_sse(row, stock_type="8", evidence_id="OFFLINE_TEST", detail=detail)
    assert cdr["security_type"] == "cdr"
    assert cdr["metadata_verified"]


@pytest.mark.parametrize("change", [{"COMPANY_CODE": "999999"}, {"A_STOCK_CODE": "999998"}, {"SEC_TYPE": "主板B"}])
def test_sse_identity_or_type_conflict_is_unknown(change):
    raw = sse_raw("900002", "8")
    detail = {"COMPANY_CODE": "900002", "A_STOCK_CODE": "900002", "SEC_TYPE": "科创A", **change}
    row = normalize_sse(raw, stock_type="8", evidence_id="OFFLINE_TEST", detail=detail)
    assert not row["metadata_verified"]
    assert row["security_type"] == "unknown"
    assert row["metadata_issues"]


def test_szse_explicit_a_tab_and_board_handle_nonstandard_code():
    result = normalize_szse(szse_raw(board="创业板"), evidence_id="OFFLINE_TEST")
    assert result["security_type"] == "ordinary_a"
    assert result["board"] == "chinext"
    assert result["name"] == "OFFLINE_TEST&"
    rows, boundary = szse_page(szse_body([szse_raw()]), requested_page=1)
    assert len(rows) == 1  # B tab is not A, never concatenated.
    assert boundary["expected_records"] == 1


def test_unknown_szse_board_is_retained_unknown():
    row = normalize_szse(szse_raw(board="新板块"), evidence_id="OFFLINE_TEST")
    assert row["code"] == "890001"
    assert row["board"] == "unknown"
    assert not row["metadata_verified"]


@pytest.mark.parametrize("kind", ["sse", "szse"])
@pytest.mark.parametrize("broken", ["truncated", "page_jump", "page_count", "boolean_total"])
def test_source_page_bounds_fail_closed(kind, broken):
    if kind == "sse":
        body = sse_body([sse_raw()], size=1)
        meta = body["pageHelp"]
        keys = ("pageNo", "pageCount", "total")
    else:
        body = szse_body([szse_raw()], size=1)
        meta = body[0]["metadata"]
        keys = ("pageno", "pagecount", "recordcount")
    if broken == "truncated":
        meta[keys[2]] = 2
        meta[keys[1]] = 2
        if kind == "sse":
            body["result"] = []; meta["data"] = []
        else:
            body[0]["data"] = []
    elif broken == "page_jump":
        meta[keys[0]] = 2
    elif broken == "page_count":
        meta[keys[1]] = 2
    else:
        meta[keys[2]] = True
    with pytest.raises(ComponentContractError):
        if kind == "sse":
            sse_page(body, stock_type="1", requested_page=1)
        else:
            szse_page(body, requested_page=1)


def test_sse_result_and_page_body_cannot_conflict():
    body = sse_body([sse_raw()]); body["pageHelp"]["data"][0]["A_STOCK_CODE"] = "000000"
    with pytest.raises(ComponentContractError, match="result_page_data_conflict"):
        sse_page(body, stock_type="1", requested_page=1)


@pytest.mark.parametrize("field,value", [("STOCK_TYPE", "2"), ("LIST_BOARD", "2")])
def test_sse_returned_type_and_board_must_match_request(field, value):
    body = sse_body([sse_raw()]); body["result"][0][field] = value; body["pageHelp"]["data"] = deepcopy(body["result"])
    with pytest.raises(ComponentContractError):
        sse_page(body, stock_type="1", requested_page=1)


def test_szse_changed_columns_or_mixed_tab_contract_block():
    body = szse_body([szse_raw()]); body[0]["metadata"]["cols"]["agdm"] = "B股代码"
    with pytest.raises(ComponentContractError, match="column_contract"):
        szse_page(body, requested_page=1)


def test_more_than_100_read_all_source_pages(tmp_path):
    calls = []
    rows = [szse_raw(f"{800000+i:06d}", "主板" if i % 2 else "创业板") for i in range(121)]
    def fetch(endpoint, params, **kwargs):
        assert endpoint == SZSE_ENDPOINT
        calls.append(params)
        n = params["PAGENO"]
        return response(szse_body(rows[(n-1)*20:n*20], page=n, pages=7, total=121))
    pages, manifests, _ = discover_exchange_lists(target=DAY, directory=tmp_path, permissions=approved("SZSE"),
                                                 mode="offline_test", transport=fetch)
    assert sum(len(p["records"]) for p in pages) == 121
    assert len(calls) == 7
    manifest = next(m for m in manifests if m["provider"] == "szse")
    assert manifest["complete"]
    assert manifest["expected_records"] == 121
    assert [p["terminal"] for p in pages] == [False]*6+[True]
    assert all(m["provenance_mode"] == "offline_test" for m in manifests)


@pytest.mark.parametrize("fault", ["boundary_changed", "duplicate_code", "network_empty"])
def test_multi_page_failures_not_complete(tmp_path, fault):
    def fetch(endpoint, params, **kwargs):
        n = params["PAGENO"]
        if fault == "network_empty" and n == 2:
            return {"ok": False, "status": "network_error", "fetched_at": STAMP}
        total = 21 if n == 1 or fault != "boundary_changed" else 22
        rows = [szse_raw(f"{800000+i:06d}") for i in range(20)] if n == 1 else [szse_raw("800000" if fault == "duplicate_code" else "800099")]
        return response(szse_body(rows, page=n, pages=2, total=total))
    _, manifests, _ = discover_exchange_lists(target=DAY, directory=tmp_path, permissions=approved("SZSE"),
                                              mode="offline_test", transport=fetch)
    result = next(m for m in manifests if m["provider"] == "szse")
    assert not result["complete"]
    assert result["errors"]


@pytest.mark.parametrize("source_day,observed", [("2026-09-10", STAMP), ("", STAMP), ("2026-09-11", "2026-09-12T00:01:00+08:00")])
def test_target_date_needs_source_and_observation_evidence(tmp_path, source_day, observed):
    def fetch(*args, **kwargs):
        return {**response(szse_body([szse_raw()], source_date=source_day)), "fetched_at": observed}
    _, manifests, _ = discover_exchange_lists(target=DAY, directory=tmp_path, permissions=approved("SZSE"),
                                              mode="offline_test", transport=fetch)
    result = next(m for m in manifests if m["provider"] == "szse")
    assert not result["complete"]
    assert result["errors"]


def test_star_cdr_details_are_traced_and_statuses_unknown(tmp_path):
    seen = []
    def fetch(endpoint, params, **kwargs):
        seen.append((endpoint, params))
        if endpoint == SSE_METADATA_ENDPOINT:
            return response({"result": [{"COMPANY_CODE": "900002", "A_STOCK_CODE": "900002", "SEC_TYPE": "科创CDR"}]})
        kind = params["STOCK_TYPE"]
        return response(sse_body([sse_raw("900001" if kind == "1" else "900002", kind)]))
    pages, manifests, _ = discover_exchange_lists(target=DAY, directory=tmp_path, permissions=approved("SSE"),
                                                 mode="offline_test", transport=fetch)
    star = next(p for p in pages if p["dataset"] == "star")["records"][0]
    assert star["security_type"] == "cdr"
    assert star["statuses"] == {}
    assert star["raw"]["detail"]["SEC_TYPE"] == "科创CDR"
    assert len(seen) == 3
    assert all(m["temporal_basis"] == "current_snapshot_as_observed" for m in manifests if m["provider"] == "sse")


def test_permissions_and_prior_access_denied_are_distinct_and_never_call_network(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled or access-denied source must not be called")
    permissions = approved("BSE")
    permissions["BSE"].update(access_status="access_denied", access_evidence="OFFLINE_TEST_403")
    pages, manifests, requests = discover_exchange_lists(target=DAY, directory=tmp_path, permissions=permissions,
                                                         mode="offline_test", transport=forbidden)
    assert pages == requests == []
    bse = next(m for m in manifests if m["provider"] == "bse")
    assert bse["permission_status"] == "approved"
    assert bse["errors"] == ["access_denied"]
    assert bse["access_evidence"] == "OFFLINE_TEST_403"


def test_no_test_transport_or_mode_can_create_online_evidence(tmp_path):
    with pytest.raises(ValueError, match="injected transport"):
        discover_exchange_lists(target=DAY, directory=tmp_path, permissions={}, transport=lambda: None)
    with pytest.raises(ValueError, match="real network"):
        discover_exchange_lists(target=DAY, directory=tmp_path, permissions={}, mode="offline_test")
    with pytest.raises(ValueError, match="research"):
        discover_exchange_lists(target=DAY, directory=tmp_path/"research", permissions={}, mode="offline_test", transport=lambda: None)
    with pytest.raises(ValueError, match="live metadata caches"):
        discover_exchange_lists(target=DAY, directory=tmp_path, permissions={}, mode="offline_test", transport=lambda: None,
                                cache_directory=tmp_path/"cache")


def test_no_historical_or_offset_parameters_are_invented(tmp_path):
    def fetch(endpoint, params, **kwargs):
        assert set(params) == {"SHOWTYPE", "CATALOGID", "TABKEY", "PAGENO"}
        return response(szse_body([szse_raw()]))
    discover_exchange_lists(target=DAY, directory=tmp_path, permissions=approved("SZSE"), mode="offline_test", transport=fetch)


def test_transport_rejects_unregistered_endpoint_without_network(tmp_path):
    transport = WebsiteTransport(tmp_path)
    with pytest.raises(ValueError, match="unregistered"):
        transport("http://query.sse.com.cn/commonQuery.do", {}, label="OFFLINE_TEST", target=DAY)
    with pytest.raises(ValueError):
        WebsiteTransport(tmp_path, spacing_seconds=0)


def test_elapsed_budget_stops_with_explicit_failure(tmp_path, monkeypatch):
    import ashare_daily.providers.exchange_universe as module
    values = iter([0, 2, 3])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(values, 3))
    _, manifests, requests = discover_exchange_lists(target=DAY, directory=tmp_path, permissions=approved("SZSE"),
        mode="offline_test", transport=lambda *a, **k: pytest.fail("elapsed budget must stop request"), max_elapsed_seconds=1)
    assert not requests
    assert "discovery_elapsed_budget_reached" in next(m for m in manifests if m["provider"] == "szse")["errors"]
