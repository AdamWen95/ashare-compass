"""Permission-gated official website list components, not licensed public APIs.

Only component parameters/fields observed in the exchanges' own page scripts are
used. Current SSE metadata is never offered as a historical constituent list.
Raw responses, including failures and metadata conflicts, remain inspectable.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
from html import unescape
import json
import math
from pathlib import Path
import re
import ssl
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler
from zoneinfo import ZoneInfo

from ashare_daily.universe import scope_boards

TZ = ZoneInfo("Asia/Shanghai")
SSE_PAGE = "https://www.sse.com.cn/assortment/stock/list/share/"
SSE_ENDPOINT = "https://query.sse.com.cn/sseQuery/commonQuery.do"
SSE_METADATA_ENDPOINT = "https://query.sse.com.cn/commonQuery.do"
SZSE_PAGE = "https://www.szse.cn/market/product/stock/list/index.html"
SZSE_ENDPOINT = "https://www.szse.cn/api/report/ShowReport/data"
BOARDS = {"sse_main": "SSE", "star": "SSE", "szse_main": "SZSE", "chinext": "SZSE", "bse": "BSE"}
COMPONENT_EVIDENCE = {
    "SSE": "https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_stocksDepositoryReceipts_2021.js",
    "SZSE": "https://res.szse.cn/modules/report/js/report_new.min.js",
}


class ComponentContractError(ValueError):
    pass


def _write(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "redirect_not_permitted", headers, fp)


class WebsiteTransport:
    """Anonymous HTTPS only; no credentials, TLS bypass, proxy rotation or retry."""
    def __init__(self, directory: Path, *, timeout_seconds: int = 20,
                 environment_label: str = "current_execution_environment", spacing_seconds: float = 0.5):
        if not 1 <= timeout_seconds <= 60 or spacing_seconds < 0.5:
            raise ValueError("bounded timeout and low-frequency spacing are required")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout_seconds
        self.environment_label = environment_label
        self.spacing = spacing_seconds
        self.last_request = 0.0
        self.count = 0
        self.stopped_hosts: set[str] = set()
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def __call__(self, endpoint: str, params: dict, *, label: str, target: date) -> dict:
        if endpoint not in {SSE_ENDPOINT, SSE_METADATA_ENDPOINT, SZSE_ENDPOINT}:
            raise ValueError("unregistered website component")
        host = urlsplit(endpoint).hostname
        if host in self.stopped_hosts:
            return {"ok": False, "status": "source_stopped", "error": "earlier_access_denied_or_rate_limited",
                    "fetched_at": datetime.now(TZ).isoformat(), "params": params}
        self.count += 1
        identifier = f"{self.count:05d}-{label}"
        url = endpoint + "?" + urlencode(params)
        wait = self.spacing - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        started = time.monotonic()
        result = {"ok": False, "url": url, "params": params, "target_date": target.isoformat(),
                  "fetched_at": datetime.now(TZ).isoformat(), "environment_label": self.environment_label,
                  "verification_kind": "live_network", "provenance_mode": "online",
                  "login": "not_required_anonymous", "attempts": 1,
                  "access_method": "official_website_component_not_public_api"}
        raw = b""
        try:
            request = Request(url, headers={"Referer": SSE_PAGE if "sse.com.cn" in host else SZSE_PAGE,
                                           "User-Agent": "ashare-daily-research/0.5 local-noncommercial"})
            with self.opener.open(request, timeout=self.timeout) as response:
                result["http_status"] = response.status
                result["content_type"] = response.headers.get("Content-Type")
                result["source_http_date"] = response.headers.get("Date")
                result["source_last_modified"] = response.headers.get("Last-Modified")
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ComponentContractError("response_size_limit")
            body = json.loads(raw.decode("utf-8-sig"))
            result.update(ok=True, status="ok", body=body)
        except HTTPError as exc:
            result.update(http_status=exc.code, status="access_denied" if exc.code in {401, 403} else
                          "rate_limited" if exc.code == 429 else "http_error", error=str(exc))
            raw = exc.read(4_000_001)
            if exc.code in {401, 403, 429}:
                # SSE's two paths are the same host, so a stop applies to both.
                self.stopped_hosts.add(host)
        except (TimeoutError, URLError, ssl.SSLError, OSError) as exc:
            result.update(status="network_error", error=f"{type(exc).__name__}:{exc}")
        except (ValueError, UnicodeError) as exc:
            result.update(status="schema_error", error=f"{type(exc).__name__}:{exc}")
        finally:
            self.last_request = time.monotonic()
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)
        result["raw_sha256"] = hashlib.sha256(raw).hexdigest()
        raw_path = self.directory / (identifier + ".raw")
        with raw_path.open("xb") as stream:
            stream.write(raw)
        result["raw_path"] = str(raw_path.resolve())
        result["response_path"] = str((self.directory / (identifier + ".json")).resolve())
        _write(Path(result["response_path"]), result)
        return result


def _integer(value, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ComponentContractError("invalid_source_" + name)
    return value


def _date(value) -> str | None:
    value = str(value or "").strip()
    if value in {"", "-"}:
        return None
    if re.fullmatch(r"\d{8}", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def sse_page(body: dict, *, stock_type: str, requested_page: int) -> tuple[list, dict]:
    if not isinstance(body, dict) or body.get("actionErrors") or body.get("fieldErrors"):
        raise ComponentContractError("sse_query_error")
    rows, meta = body.get("result"), body.get("pageHelp")
    if not isinstance(rows, list) or not isinstance(meta, dict):
        raise ComponentContractError("sse_missing_rows_or_pageHelp")
    if body.get("sqlId") != "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L":
        raise ComponentContractError("sse_dataset_mismatch")
    if "data" in meta and rows != meta["data"]:
        raise ComponentContractError("sse_result_page_data_conflict")
    paging = _paging(rows, meta, "pageNo", "pageSize", "pageCount", "total", requested_page)
    paging["source_as_of_date"] = _date(body.get("queryDate"))
    for row in rows:
        if not isinstance(row, dict):
            raise ComponentContractError("invalid_row")
        if row.get("STOCK_TYPE") != stock_type:
            raise ComponentContractError("sse_requested_type_conflict")
        if row.get("LIST_BOARD") != {"1": "1", "8": "2"}[stock_type]:
            raise ComponentContractError("sse_board_conflict")
    return rows, paging


def szse_page(body: list, *, requested_page: int) -> tuple[list, dict]:
    if not isinstance(body, list):
        raise ComponentContractError("szse_missing_report_array")
    tabs = [tab for tab in body if isinstance(tab, dict) and tab.get("metadata", {}).get("tabkey") == "tab1"]
    if len(tabs) != 1:
        raise ComponentContractError("szse_a_tab_missing_or_duplicate")
    tab = tabs[0]
    meta, rows = tab["metadata"], tab.get("data")
    if meta.get("catalogid") != "1110" or meta.get("name") != "A股列表":
        raise ComponentContractError("szse_type_or_catalog_conflict")
    cols = meta.get("cols", {})
    if cols.get("agdm") != "A股代码" or cols.get("bk") != "板块":
        raise ComponentContractError("szse_column_contract_changed")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ComponentContractError("szse_invalid_rows")
    paging = _paging(rows, meta, "pageno", "pagesize", "pagecount", "recordcount", requested_page)
    paging["source_as_of_date"] = _date(meta.get("subname"))
    return rows, paging


def _paging(rows, meta, page_key, size_key, pages_key, total_key, expected):
    page = _integer(meta.get(page_key), "page", 1)
    size = _integer(meta.get(size_key), "page_size", 1)
    pages = _integer(meta.get(pages_key), "page_count", 1)
    total = _integer(meta.get(total_key), "total")
    if page != expected or pages != max(1, math.ceil(total / size)) or page > pages:
        raise ComponentContractError("page_boundary_conflict")
    count = min(size, max(0, total - (page - 1) * size))
    if len(rows) != count:
        raise ComponentContractError("page_truncated_or_excess")
    return {"page_number": page, "page_size": size, "expected_pages": pages,
            "expected_records": total, "terminal": page == pages}


def normalize_sse(raw: dict, *, stock_type: str, evidence_id: str, detail: dict | None = None) -> dict:
    code = str(raw.get("A_STOCK_CODE", "")).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ComponentContractError("sse_missing_security_code")
    board = {"1": "sse_main", "8": "star"}[stock_type]
    kind = "ordinary_a" if stock_type == "1" else "unknown"
    issues = []
    if detail is not None:
        if detail.get("COMPANY_CODE") != raw.get("COMPANY_CODE") or detail.get("A_STOCK_CODE") != code:
            issues.append("sse_detail_identity_conflict")
            kind = "unknown"
        else:
            kind = {"科创A": "ordinary_a", "科创CDR": "cdr", "主板A": "ordinary_a",
                    "主板B": "b_share"}.get(detail.get("SEC_TYPE"), "unknown")
            expected_types = {"主板A"} if stock_type == "1" else {"科创A", "科创CDR"}
            if detail.get("SEC_TYPE") not in expected_types:
                issues.append("sse_detail_type_conflict")
                kind = "unknown"
    elif stock_type == "8":
        issues.append("star_share_class_requires_SEC_TYPE")
    return {"provider": "sse", "exchange": "SSE", "board": board, "code": code,
            "name": str(raw.get("SEC_NAME_CN") or ""), "security_type": kind,
            "metadata_verified": kind != "unknown" and not issues,
            "metadata_source": COMPONENT_EVIDENCE["SSE"], "evidence_id": evidence_id,
            "listing_date": _date(raw.get("LIST_DATE")), "delisting_date": _date(raw.get("DELIST_DATE")),
            "listing_status": "listed", "statuses": {},
            "metadata_issues": issues, "raw": {"listing": raw, "detail": detail}}


def normalize_szse(raw: dict, *, evidence_id: str) -> dict:
    code = str(raw.get("agdm", "")).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ComponentContractError("szse_missing_security_code")
    board = {"主板": "szse_main", "创业板": "chinext"}.get(raw.get("bk"), "unknown")
    return {"provider": "szse", "exchange": "SZSE", "board": board, "code": code,
            "name": unescape(re.sub(r"<[^>]*>", "", str(raw.get("agjc") or ""))),
            "security_type": "ordinary_a", "metadata_verified": board != "unknown",
            "metadata_source": COMPONENT_EVIDENCE["SZSE"], "evidence_id": evidence_id,
            "listing_date": _date(raw.get("agssrq")), "listing_status": "listed", "statuses": {},
            "metadata_issues": [] if board != "unknown" else ["szse_board_unknown"], "raw": raw}


def discover_exchange_lists(*, target: date, directory: Path, permissions: dict,
                            timeout_seconds: int = 20, mode: str = "online",
                            environment_label: str = "current_execution_environment",
                            transport: Callable | None = None,
                            enrich_star: bool = True, cache_directory: Path | None = None,
                            max_elapsed_seconds: int = 900, scope: str = "all_a") -> tuple[list, list, list]:
    """Return existing universe pages/manifests/requests; never another stock pool.

    permissions maps SSE/SZSE/BSE to enabled/permission_status/access_status.
    BSE remains explicit contract/access blocked until its real component can be
    inspected. No request to a BSE data endpoint is invented here.
    """
    required_boards = scope_boards(scope)
    directory = Path(directory)
    if mode not in {"online", "offline_test"}:
        raise ValueError("invalid provenance mode")
    if transport is not None and mode != "offline_test":
        raise ValueError("injected transport cannot produce online evidence")
    if mode == "offline_test" and "research" in {p.lower() for p in directory.parts}:
        raise ValueError("offline data cannot enter research")
    if mode == "offline_test" and transport is None:
        raise ValueError("offline mode must not use the real network")
    if not 1 <= max_elapsed_seconds <= 1800:
        raise ValueError("discovery requires a bounded elapsed-time budget")
    if mode == "offline_test" and cache_directory is not None:
        raise ValueError("test transports cannot populate live metadata caches")
    directory.mkdir(parents=True, exist_ok=True)
    if transport is not None:
        fetch = transport
    elif cache_directory is not None:
        from ashare_daily.providers.exchange_cache import CachedWebsiteTransport
        fetch = CachedWebsiteTransport(directory / "requests", cache_root=cache_directory,
                                       timeout_seconds=timeout_seconds, environment_label=environment_label)
    else:
        fetch = WebsiteTransport(directory / "requests", timeout_seconds=timeout_seconds,
                                 environment_label=environment_label)
    pages, manifests, requests = [], [], []
    started = time.monotonic()
    for exchange, dataset, stock_type, coverage in [
        ("SSE", "main_a", "1", ["sse_main"]), ("SSE", "star", "8", ["star"]),
        ("SZSE", "a_shares", None, ["szse_main", "chinext"]), ("BSE", "listed_shares", None, ["bse"]),
    ]:
        if not set(coverage).intersection(required_boards):
            # This is an explicit user-selected scope, never a response to a
            # failed source. all_a continues to require the BSE manifest.
            continue
        permission = permissions.get(exchange, {})
        observed = datetime.now(TZ).isoformat()
        manifest = {"provider": exchange.lower(), "dataset": dataset,
                    "permission_status": permission.get("permission_status", "unconfirmed"),
                    "observed_at": observed, "as_of_date": None, "provenance_mode": mode,
                    "lineage_id": exchange.lower() + "_official_website", "authoritative": True,
                    "coverage_boards": coverage, "expected_pages": None, "expected_records": None,
                    "complete": False, "errors": [], "source_as_of_date": None,
                    "source_business_date": None, "scope": scope,
                    "temporal_basis": "current_snapshot_as_observed", "access_method": "official_website_component_not_public_api"}
        manifests.append(manifest)
        if permission.get("enabled") is not True or permission.get("permission_status") != "approved":
            manifest["errors"].append("usage_permission_unconfirmed_or_disabled")
            continue
        if permission.get("access_status") in {"access_denied", "rate_limited", "unverified"}:
            manifest["errors"].append(permission["access_status"])
            manifest["access_evidence"] = permission.get("access_evidence")
            continue
        if exchange == "BSE":
            manifest["errors"].append("bse_website_component_contract_unverified")
            continue
        if mode == "online" and target != datetime.now(TZ).date():
            manifest["errors"].append("current_list_cannot_reconstruct_historical_date")
            continue
        parts, seen, baseline, details_stopped = [], set(), None, False
        try:
            for number in range(1, 2049):  # Safety bound; reaching it fails closed, never truncates successfully.
                if time.monotonic() - started > max_elapsed_seconds:
                    raise ComponentContractError("discovery_elapsed_budget_reached")
                if exchange == "SSE":
                    params = {"STOCK_TYPE": stock_type, "REG_PROVINCE": "", "CSRC_CODE": "", "STOCK_CODE": "",
                              "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L", "COMPANY_STATUS": "2,4,5,7,8",
                              "type": "inParams", "isPagination": "true", "pageHelp.cacheSize": 1,
                              "pageHelp.beginPage": number, "pageHelp.pageSize": 25, "pageHelp.pageNo": number}
                    endpoint = SSE_ENDPOINT
                else:
                    params = {"SHOWTYPE": "JSON", "CATALOGID": "1110", "TABKEY": "tab1", "PAGENO": number}
                    endpoint = SZSE_ENDPOINT
                response = fetch(endpoint, params, label=f"{exchange.lower()}-{dataset}-{number:04d}", target=target)
                requests.append({k: v for k, v in response.items() if k != "body"})
                manifest["observed_at"] = response["fetched_at"]
                if not response.get("ok"):
                    raise ComponentContractError(response.get("status", "request_failed"))
                raw_rows, paging = (sse_page(response["body"], stock_type=stock_type, requested_page=number)
                                    if exchange == "SSE" else szse_page(response["body"], requested_page=number))
                bounds = tuple(paging[k] for k in ("page_size", "expected_pages", "expected_records", "source_as_of_date"))
                if baseline is not None and bounds != baseline:
                    raise ComponentContractError("source_boundary_changed_during_read")
                baseline = bounds
                manifest.update({k: paging[k] for k in ("expected_pages", "expected_records", "source_as_of_date")})
                manifest["source_business_date"] = paging["source_as_of_date"]
                source_date = paging["source_as_of_date"]
                actual_day = datetime.fromisoformat(response["fetched_at"]).astimezone(TZ).date()
                if source_date and source_date != target.isoformat():
                    raise ComponentContractError("source_not_updated_for_target_date")
                if actual_day != target:
                    raise ComponentContractError("current_list_observation_date_mismatch")
                if exchange == "SZSE" and not source_date:
                    raise ComponentContractError("source_date_missing")
                manifest["as_of_date"] = target.isoformat()
                manifest["temporal_basis"] = "source_explicit_current_date" if source_date else "current_snapshot_as_observed"
                records = []
                for raw in raw_rows:
                    detail = None
                    detail_hash = ""
                    if stock_type == "8" and enrich_star and not details_stopped:
                        if time.monotonic() - started > max_elapsed_seconds:
                            raise ComponentContractError("discovery_elapsed_budget_reached")
                        detail_response = fetch(SSE_METADATA_ENDPOINT, {"isPagination": "false",
                            "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GPGK_GSGK_C", "COMPANY_CODE": raw.get("COMPANY_CODE", "")},
                            label=f"sse-star-type-{raw.get('COMPANY_CODE', 'unknown')}", target=target)
                        requests.append({k: v for k, v in detail_response.items() if k != "body"})
                        data = detail_response.get("body", {})
                        detail_rows = data.get("result") if isinstance(data, dict) else None
                        detail_day = _date(detail_response.get("fetched_at", "")[:10])
                        if (detail_response.get("ok") and isinstance(data, dict) and not data.get("actionErrors")
                            and not data.get("fieldErrors") and isinstance(detail_rows, list) and len(detail_rows) == 1
                            and isinstance(detail_rows[0], dict) and detail_day == target.isoformat()):
                            detail = detail_rows[0]
                            detail_hash = detail_response.get("raw_sha256", "")
                        elif detail_response.get("status") in {"access_denied", "rate_limited", "source_stopped"}:
                            details_stopped = True
                    evidence = response.get("raw_sha256", "") + (":" + detail_hash if detail_hash else "")
                    row = (normalize_sse(raw, stock_type=stock_type, evidence_id=evidence, detail=detail)
                           if exchange == "SSE" else normalize_szse(raw, evidence_id=evidence))
                    if row["code"] in seen:
                        raise ComponentContractError("duplicate_security_across_pages")
                    seen.add(row["code"])
                    records.append(row)
                part = {"provider": exchange.lower(), "dataset": dataset, "page_number": number,
                        "records": records, "terminal": paging["terminal"], "source_boundary": paging,
                        "raw_response_path": response.get("response_path"), "raw_sha256": response.get("raw_sha256")}
                pages.append(part)
                parts.append(part)
                if paging["terminal"]:
                    manifest["complete"] = sum(len(p["records"]) for p in parts) == paging["expected_records"]
                    break
            else:
                raise ComponentContractError("page_safety_bound_reached")
        except (ComponentContractError, KeyError, TypeError, ValueError) as exc:
            manifest["complete"] = False
            manifest["errors"].append(str(exc))
    _write(directory / "exchange-manifests.json", manifests)
    _write(directory / "exchange-pages.json", pages)
    _write(directory / "exchange-requests.json", requests)
    return pages, manifests, requests
