"""Sina sector discovery and dated lightweight observations for sector-first F2.

Public website paths are not a general-purpose licensed API. Live access requires
the recorded personal/local authorization; source-grant status remains explicit.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import queue
import re
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode, urlsplit, parse_qs
from zoneinfo import ZoneInfo

from .base import SecurityIdentity, iso_date
from ..operations.backup import _io


SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_BYTES = 2_000_000
CATALOG_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
COUNT_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
MEMBERS_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
QUOTE_BASE = "https://hq.sinajs.cn/list="
TAXONOMY = "sina_industry"
TAXONOMY_ID = "sina:sina_industry:current"
ACCESS_STOPS = {"permission_denied", "rate_limited"}
UA = "ashare-daily-research/0.5 (personal noncommercial local research; bounded source client)"


def _now():
    return datetime.now(SHANGHAI).isoformat()


def _save(path: Path, value):
    with _io(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def allowed_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 6000:
        raise ValueError("invalid bounded source URL")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or parts.port or parts.fragment:
        raise ValueError("only exact anonymous HTTPS source paths are allowed")
    if url == CATALOG_URL:
        return url
    if parts.scheme + "://" + parts.netloc + parts.path in {COUNT_URL, MEMBERS_URL}:
        parameters = parse_qs(parts.query, keep_blank_values=True)
        if any(len(values) != 1 for values in parameters.values()):
            raise ValueError("duplicate source parameter")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,60}", parameters.get("node", [""])[0]):
            raise ValueError("invalid source sector label")
        if parts.path.endswith("getHQNodeStockCount") and set(parameters) == {"node"}:
            return url
        if set(parameters) == {"page", "num", "sort", "asc", "node", "symbol", "_s_r_a"}:
            if (parameters["num"] == ["80"] and parameters["sort"] == ["symbol"] and parameters["asc"] == ["1"]
                    and parameters["symbol"] == [""] and parameters["_s_r_a"] == ["page"]
                    and parameters["page"][0].isdigit() and 1 <= int(parameters["page"][0]) <= 100):
                return url
    if url.startswith(QUOTE_BASE):
        symbols = url[len(QUOTE_BASE):].split(",")
        if 1 <= len(symbols) <= 80 and len(symbols) == len(set(symbols)) and all(re.fullmatch(r"(?:sh|sz)[0-9]{6}", item) for item in symbols):
            return url
    patterns = (
        r"https://finance\.sina\.com\.cn/realstock/company/(?:sh|sz)[0-9]{6}/(?:hisdata_klc2/klc_kl|qfq)\.js",
        r"https://quotes\.sina\.cn/cn/api/jsonp\.php/var%20_((?:sh|sz)[0-9]{6})[0-9]{4}_[0-9]{2}_[0-9]{2}=/KC_MarketDataService\.getKLineData\?symbol=\1",
    )
    if any(re.fullmatch(pattern, url) for pattern in patterns):
        return url
    raise ValueError("URL outside confirmed Sina source paths")


class SinaHttpClient:
    """Serial, reusable TLS worker; each request has a hard deadline, no retry."""
    def __init__(self, output_directory: Path, *, mode="research", transport=None, permission=None,
                 timeout_seconds=15, pause_seconds=2, max_requests=1000):
        if mode not in {"research", "offline_test"} or (mode == "research" and transport is not None):
            raise ValueError("live/test transport isolation required")
        if mode == "offline_test" and transport is None:
            raise ValueError("offline_test requires explicit fixture transport")
        if (isinstance(timeout_seconds, bool) or isinstance(pause_seconds, bool)
                or not math.isfinite(timeout_seconds) or not math.isfinite(pause_seconds)
                or not .01 <= timeout_seconds <= 30 or not 0 <= pause_seconds <= 5
                or type(max_requests) is not int or not 1 <= max_requests <= 2000):
            raise ValueError("invalid bounded source limits")
        self.directory = Path(output_directory).resolve()
        if mode == "offline_test" and "research" in {part.casefold() for part in self.directory.parts}:
            raise ValueError("offline_test cannot write research paths")
        _io(self.directory).mkdir(parents=True, exist_ok=True)
        self.mode, self.transport, self.permission = mode, transport, deepcopy(permission or {})
        self.timeout_seconds, self.pause_seconds, self.max_requests = timeout_seconds, pause_seconds, max_requests
        self.calls, self.responses, self.stopped = 0, [], None
        self._process, self._events, self._last = None, None, None
        self._busy = threading.Lock()
        self._network_failures = 0
        self.deadline_monotonic = None

    def get(self, url, *, operation="source"):
        allowed_url(url)
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("Sina source concurrency is prohibited")
        try:
            authorized = (self.permission.get("enabled") is True
                          and self.permission.get("purpose") == "personal_noncommercial_local_research"
                          and self.permission.get("user_authorized") is True
                          and self.permission.get("permission_status") == "approved"
                          and isinstance(self.permission.get("permission_basis"), str)
                          and bool(self.permission["permission_basis"].strip())
                          and self.permission.get("permitted_storage") is True
                          and self.permission.get("permitted_automated_access") is True
                          and self.permission.get("llm_export") is False)
            remaining = None
            if self.deadline_monotonic is not None:
                if isinstance(self.deadline_monotonic, bool) or not math.isfinite(self.deadline_monotonic):
                    raise ValueError("invalid_overall_deadline")
                remaining = self.deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    self.stopped = "runtime_limit_exhausted"
                    self.close()
            if not authorized or self.stopped or self.calls >= self.max_requests:
                status = "permission_required" if not authorized else "runtime_limit_exhausted" if self.stopped == "runtime_limit_exhausted" else "circuit_open" if self.stopped else "request_budget_exhausted"
                return {"url": url, "ok": False, "status": status, "body_complete": False, "fetched_at": _now(),
                        "verification_kind": "not_requested", "metrics": {"requests": 0, "retries": 0}, "evidence_path": None}
            if self._last is not None:
                delay = self.pause_seconds - (time.monotonic() - self._last)
                if delay > 0:
                    if remaining is not None and delay >= remaining:
                        self.stopped = "runtime_limit_exhausted"
                        self.close()
                        return {"url": url, "ok": False, "status": "runtime_limit_exhausted", "body_complete": False,
                            "fetched_at": _now(), "verification_kind": "not_requested",
                            "metrics": {"requests": 0, "retries": 0}, "evidence_path": None}
                    time.sleep(delay)
            self._last = time.monotonic()
            self.calls += 1
            if self.mode == "offline_test":
                result = deepcopy(self.transport(url))
                if result.get("verification_kind") != "offline_test":
                    raise ValueError("fixture transport must explicitly mark offline_test")
            else:
                result = self._live(url)
            if result.get("url") != url:
                raise ValueError("HTTP response request identity differs")
            if result.get("verification_kind") == "not_requested":
                self.calls -= 1
                result.update(metrics={"requests": 0, "retries": 0, "network_requests": 0}, evidence_path=None)
                return result
            result.update(mode=self.mode, provenance_mode="online" if self.mode == "research" else "offline_test",
                          operation=operation, upstream_grant_status=self.permission.get("upstream_grant_status", "unconfirmed"))
            result["metrics"] = {"requests": 1, "retries": 0, "network_requests": int(self.mode == "research"),
                                 "elapsed_seconds": result.get("elapsed_seconds"), "bytes": result.get("body_bytes", 0)}
            if result.get("status") in ACCESS_STOPS:
                self.stopped = result["status"]
                self.close()
            elif result.get("status") in {"network_error", "timeout", "worker_failed"}:
                self._network_failures += 1
                if self._network_failures >= 2:
                    self.stopped = "repeated_network_failure"
                    self.close()
            else:
                self._network_failures = 0
            raw = base64.b64decode(result.get("body_base64", ""), validate=True)
            if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != result.get("body_sha256"):
                raise ValueError("source HTTP bytes/hash mismatch")
            token = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f") + f"-{self.calls:04d}"
            body_path, evidence_path = self.directory / (token + ".bin"), self.directory / (token + ".json")
            with _io(body_path).open("xb") as stream:
                stream.write(raw)
            result.update(body_path=str(body_path), evidence_path=str(evidence_path))
            _save(evidence_path, result)
            self.responses.append({"path": str(evidence_path), "sha256": hashlib.sha256(_io(evidence_path).read_bytes()).hexdigest(),
                                   "url": url, "operation": operation, "status": result["status"], "body_sha256": result["body_sha256"],
                                   "bytes": len(raw), "fetched_at": result["fetched_at"]})
            return result
        finally:
            self._busy.release()

    def _start(self):
        self.close()
        options = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
                   "text": True, "encoding": "utf-8", "bufsize": 1}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m", "ashare_daily.providers.sina_sectors", "--http-worker"], **options)
        self._events = queue.Queue()
        process, events = self._process, self._events
        def reader():
            try:
                while line := process.stdout.readline(4_000_001):
                    if len(line) > 4_000_000:
                        break
                    events.put(line)
            finally:
                events.put(None)
        threading.Thread(target=reader, daemon=True).start()

    def _live(self, url):
        started, stages = time.monotonic(), []
        request_timeout = self.timeout_seconds
        if self.deadline_monotonic is not None:
            request_timeout = min(request_timeout, self.deadline_monotonic - started)
            if request_timeout <= 0:
                self.stopped = "runtime_limit_exhausted"
                return {"url": url, "ok": False, "status": "runtime_limit_exhausted", "http_status": None,
                    "body_base64": "", "body_sha256": hashlib.sha256(b"").hexdigest(), "body_bytes": 0,
                    "body_complete": False, "fetched_at": _now(), "verification_kind": "not_requested",
                    "elapsed_seconds": 0, "stages": []}
        result = {"url": url, "ok": False, "status": "worker_failed", "http_status": None, "headers": {},
                  "body_base64": "", "body_sha256": hashlib.sha256(b"").hexdigest(), "body_bytes": 0, "body_complete": False}
        try:
            if self._process is None or self._process.poll() is not None:
                self._start()
            self._process.stdin.write(json.dumps({"id": self.calls, "url": url, "timeout": request_timeout}) + "\n")
            self._process.stdin.flush()
            while True:
                remaining = request_timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise queue.Empty
                line = self._events.get(timeout=remaining)
                if line is None:
                    raise ValueError("source worker closed before response")
                event = json.loads(line)
                if event.get("id") != self.calls:
                    raise ValueError("source worker identity mismatch")
                if "stage" in event:
                    stages.append(event)
                elif "result" in event:
                    result = event["result"]
                    break
                else:
                    raise ValueError("source worker protocol changed")
        except queue.Empty:
            result.update(status="timeout", error="hard request deadline; worker terminated")
            if self.deadline_monotonic is not None and time.monotonic() >= self.deadline_monotonic:
                self.stopped = "runtime_limit_exhausted"
            self.close()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result.update(status="network_error", error=str(exc)[:300])
            self.close()
        result.update(fetched_at=_now(), elapsed_seconds=round(time.monotonic() - started, 6),
                      verification_kind="live_network", stages=stages,
                      failure_stage=stages[-1]["stage"] if stages and not result["ok"] else None)
        return result

    def close(self):
        process, self._process = self._process, None
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


def _http_worker():
    connections = {}
    for line in sys.stdin:
        query = json.loads(line)
        url = urlsplit(allowed_url(query["url"]))
        raw = b""
        result = {"url": query["url"], "ok": False, "http_status": None, "headers": {}, "status": "not_requested", "body_complete": False}
        connection = connections.get(url.hostname)
        def emit(stage):
            print(json.dumps({"id": query["id"], "stage": stage}), flush=True)
        started = time.monotonic()
        try:
            emit("connect")
            if connection is None:
                connection = http.client.HTTPSConnection(url.hostname, timeout=min(5, query["timeout"]), context=ssl.create_default_context())
                connections[url.hostname] = connection
            if connection.sock is None:
                connection.connect()
            connection.sock.settimeout(max(.001, query["timeout"] - (time.monotonic() - started)))
            emit("request_headers")
            headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
            if url.hostname == "hq.sinajs.cn":
                # The public LeekFund client declares this Sina origin before
                # its first request. It is never changed/retried after refusal.
                headers["Referer"] = "http://finance.sina.com.cn/"
            connection.request("GET", url.path + ("?" + url.query if url.query else ""), headers=headers)
            emit("response_headers")
            response = connection.getresponse()
            result["http_status"] = response.status
            result["headers"] = {k.lower(): v for k, v in response.getheaders() if k.lower() in {"content-type", "content-length", "content-encoding", "date", "retry-after", "location"}}
            if response.status in {401, 403, 429}:
                result["status"] = "rate_limited" if response.status == 429 else "permission_denied"
            else:
                emit("read_body")
                while len(raw) <= MAX_BYTES:
                    remaining = query["timeout"] - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError("body deadline")
                    if connection.sock is not None:
                        connection.sock.settimeout(remaining)
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - len(raw)))
                    if not chunk:
                        result["body_complete"] = True
                        break
                    raw += chunk
                length = result["headers"].get("content-length")
                if len(raw) > MAX_BYTES:
                    raw = raw[:MAX_BYTES]
                    result["status"] = "body_limit_exceeded"
                elif result["headers"].get("content-encoding", "identity") not in {"", "identity"}:
                    result["status"] = "unsupported_content_encoding"
                elif length is not None and (not length.isdigit() or int(length) != len(raw)):
                    result["status"] = "body_length_mismatch"
                    result["body_complete"] = False
                elif response.status == 200:
                    result.update(ok=True, status="received")
                else:
                    result["status"] = "redirect_refused" if 300 <= response.status < 400 else "http_error"
        except (OSError, http.client.HTTPException) as exc:
            result.update(status="timeout" if isinstance(exc, TimeoutError) else "network_error", error=str(exc)[:300])
        finally:
            if not result["ok"] and connection is not None:
                connections.pop(url.hostname, None)
                connection.close()
            result.update(fetched_at=_now(), body_base64=base64.b64encode(raw).decode("ascii"), body_bytes=len(raw),
                          body_sha256=hashlib.sha256(raw).hexdigest())
            print(json.dumps({"id": query["id"], "result": result}, ensure_ascii=False), flush=True)


def _strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("nonfinite_json_number")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _body(response):
    if response.get("ok") is not True or response.get("http_status") != 200 or response.get("body_complete") is not True:
        raise ValueError("source_" + str(response.get("status", "unavailable")))
    raw = base64.b64decode(response.get("body_base64", ""), validate=True)
    if (not raw or len(raw) > MAX_BYTES or len(raw) != response.get("body_bytes")
            or hashlib.sha256(raw).hexdigest() != response.get("body_sha256")):
        raise ValueError("source_body_hash_or_size_invalid")
    stamp = datetime.fromisoformat(response["fetched_at"])
    if stamp.utcoffset() is None or stamp > datetime.now(SHANGHAI):
        raise ValueError("source_observation_time_invalid")
    if response.get("mode") not in {"research", "offline_test"}:
        raise ValueError("source_mode_missing")
    if response.get("verification_kind") != ("live_network" if response["mode"] == "research" else "offline_test"):
        raise ValueError("source_mode_provenance_conflict")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gb18030")


def _decimal(value, *, nonnegative=False, positive=False):
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise ValueError("numeric_field_missing")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid_numeric_field") from exc
    if not parsed.is_finite() or (nonnegative and parsed < 0) or (positive and parsed <= 0):
        raise ValueError("numeric_field_out_of_range")
    return parsed


def _number_text(value):
    return format(value, "f")


def parse_catalog(response):
    """Parse a complete static assignment, never evaluate source JavaScript."""
    if response.get("url") != CATALOG_URL:
        raise ValueError("catalog_endpoint_mismatch")
    match = re.fullmatch(r"\s*var\s+S_Finance_bankuai_sinaindustry\s*=\s*(\{.*\})\s*;?\s*", _body(response), re.S)
    if not match:
        raise ValueError("catalog_assignment_boundary_invalid")
    raw = _strict_json(match[1])
    if not isinstance(raw, dict) or not raw or len(raw) > 500:
        raise ValueError("catalog_document_shape_invalid")
    rows = []
    for key, value in raw.items():
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,60}", key):
            raise ValueError("catalog_label_invalid")
        fields = value.split(",")
        if len(fields) != 13 or fields[0] != key or not fields[1]:
            raise ValueError("catalog_row_contract_changed")
        count = _decimal(fields[2], nonnegative=True)
        if count != count.to_integral_value():
            raise ValueError("catalog_count_not_integer")
        rows.append({"sector_id": "sina:" + key, "provider_id": key, "name": fields[1],
            "taxonomy": TAXONOMY, "taxonomy_id": TAXONOMY_ID,
            # The source directory contains the explicitly named new-stock basket.
            "kind": "other" if key == "new_stock" else "industry", "displayed_count": int(count),
            "source_change_pct": _number_text(_decimal(fields[5])),
            "source_amount_cny": None, "source_amount_raw": fields[7], "source_volume_raw": fields[6],
            "source_amount_unit": "unverified", "source_volume_unit": "unverified",
            "date_verified": False, "source_business_date": None,
            "date_basis": "current_undated_source_observation", "raw_fields": fields,
            "summary_issues": ["source_business_date_missing", "source_summary_units_unverified"]})
    return rows


def count_url(node):
    return allowed_url(COUNT_URL + "?" + urlencode({"node": node}))


def members_url(node, page):
    return allowed_url(MEMBERS_URL + "?" + urlencode({"page": page, "num": 80, "sort": "symbol", "asc": 1,
        "node": node, "symbol": "", "_s_r_a": "page"}))


def _source_count(response):
    value = _strict_json(_body(response))
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"[0-9]{1,6}", str(value)):
        raise ValueError("source_count_invalid")
    return int(value)


def parse_members(response):
    values = _strict_json(_body(response))
    if not isinstance(values, list) or len(values) > 80:
        raise ValueError("membership_page_contract_changed")
    rows = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("membership_row_not_object")
        symbol, code = value.get("symbol"), value.get("code")
        issues = []
        if not isinstance(symbol, str) or not symbol or not isinstance(code, str):
            issues.append("membership_symbol_or_code_missing")
        mapped = re.fullmatch(r"(sh|sz|bj)([0-9]{6})", symbol or "")
        exchange = {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}.get(mapped[1]) if mapped else None
        if not mapped:
            issues.append("membership_symbol_mapping_unknown")
        elif mapped[2] != code:
            issues.append("membership_symbol_code_conflict")
        # Prefix here maps the vendor symbol only. Master metadata classifies type/board.
        rows.append({"symbol": symbol, "code": code, "exchange": exchange, "name": value.get("name"),
            "security_type": None, "listing_board": None, "metadata_verified": False,
            "source_business_date": None, "issues": issues, "source_record": value})
    return rows


def _identity(value):
    if isinstance(value, SecurityIdentity):
        return value
    if not isinstance(value, dict) or value.get("metadata_conflict"):
        raise ValueError("source_verified_identity_required")
    return SecurityIdentity(**{key: value[key] for key in (
        "security_id", "code", "exchange", "board", "scope", "metadata_verified", "security_type") if key in value})


def _quote_symbol(identity):
    return {"SSE": "sh", "SZSE": "sz"}[identity.exchange] + identity.code


def parse_quotes(response, identities, target_date):
    target = iso_date(target_date)
    known = {_quote_symbol(_identity(item)): _identity(item) for item in identities}
    if not known or response.get("url") != QUOTE_BASE + ",".join(known):
        raise ValueError("quote_request_identity_mismatch")
    text, cursor, rows, seen = _body(response), 0, [], set()
    pattern = re.compile(r'\s*var\s+hq_str_((?:sh|sz)[0-9]{6})="([^"\r\n\\]*)";\s*')
    observed = datetime.fromisoformat(response["fetched_at"]).astimezone(SHANGHAI)
    if target > observed.date():
        raise ValueError("quote_target_after_source_observation")
    while cursor < len(text):
        match = pattern.match(text, cursor)
        if not match:
            raise ValueError("quote_assignment_boundary_invalid")
        cursor = match.end()
        symbol = match[1]
        if symbol not in known or symbol in seen:
            raise ValueError("quote_duplicate_or_unrequested_identity")
        seen.add(symbol)
        identity, fields, issues = known[symbol], match[2].split(","), []
        row = {"symbol": symbol, "security_id": identity.security_id, "identity": asdict(identity),
            "provider": "sina", "trade_date": None, "quote_at": None, "quote_time": None,
            "after_close": False, "date_verified": False, "close": None, "reference_price": None,
            "change_pct": None, "amount_cny": None, "volume_shares": None,
            "percentage_basis": "source_reference_price", "amount_unit": "CNY", "price_unit": "CNY/share",
            "volume_unit": "shares", "is_closing_daily_bar": False, "tradestatus": None, "is_st": None,
            "status_complete": False, "status_issues": ["trading_status_unknown", "st_status_unknown"],
            "source_business_date": None, "fetched_at": response["fetched_at"], "source_fields": fields,
            "issues": issues}
        if not 32 <= len(fields) <= 40:
            issues.append("quote_fields_missing_or_changed")
            rows.append(row)
            continue
        row["name"] = fields[0]
        try:
            source_date = iso_date(fields[30])
            if not re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}", fields[31]):
                raise ValueError("source_time_invalid")
            stamp = datetime.fromisoformat(fields[30] + "T" + fields[31]).replace(tzinfo=SHANGHAI)
            row.update(trade_date=source_date.isoformat(), source_business_date=source_date.isoformat(),
                quote_at=stamp.isoformat(), quote_time=fields[31],
                after_close=fields[31] >= "15:00:00" and observed >= datetime.combine(target, datetime.min.time(), SHANGHAI).replace(hour=15, minute=30))
            row.update(normalization_version="sina-dated-light-quote-v2",
                timestamp_basis="source_last_quote_update_with_actual_observation_after_1530",
                amount_basis="Sina_reported_quote_amount_CNY", afterhours_amount_inclusion="unconfirmed",
                full_day_turnover_verified=False)
            if source_date != target:
                issues.append("quote_date_mismatch")
            if stamp > observed:
                issues.append("quote_timestamp_after_observation")
            if not row["after_close"]:
                issues.append("quote_before_post_close_boundary")
            row["date_verified"] = not issues
        except (ValueError, TypeError):
            issues.append("quote_source_date_or_time_invalid")
        try:
            reference, close = _decimal(fields[2], positive=True), _decimal(fields[3], positive=True)
            row.update(reference_price=_number_text(reference), close=_number_text(close),
                change_pct=_number_text((close / reference - 1) * 100))
        except ValueError as exc:
            issues.append("quote_price_" + str(exc))
        try:
            amount, volume = _decimal(fields[9], nonnegative=True), _decimal(fields[8], nonnegative=True)
            if volume != volume.to_integral_value():
                raise ValueError("volume_not_integral_shares")
            row.update(amount_cny=_number_text(amount), volume_shares=int(volume))
        except ValueError as exc:
            issues.append("quote_volume_or_amount_" + str(exc))
        rows.append(row)
    return rows, sorted(set(known) - seen)


class SinaSectorProvider:
    """One current industry taxonomy, complete membership, dated one-day quotes."""
    def __init__(self, output_directory, *, mode="research", transport=None, permission=None,
                 timeout_seconds=15, pause_seconds=2, max_requests=1000):
        self.client = SinaHttpClient(Path(output_directory), mode=mode, transport=transport, permission=permission,
            timeout_seconds=timeout_seconds, pause_seconds=pause_seconds, max_requests=max_requests)
        self.mode = mode
        self.started_at = _now()

    @property
    def blocked(self):
        return bool(self.client.stopped) or self.client.calls >= self.client.max_requests

    def close(self):
        self.client.close()

    def _packet(self, operation, start, rows, issues, *, boundary=False, **values):
        complete = boundary and not issues
        return {"provider": "sina", "operation": operation, "schema_version": "sina-sectors-v1",
            "taxonomy": TAXONOMY, "taxonomy_id": TAXONOMY_ID, "status": "ok" if complete else "blocked",
            "complete": complete, "boundary_verified": boundary, "rows": rows, "issues": sorted(set(issues)),
            "fetched_at": _now(), "source_business_date": None,
            "provenance_mode": "online" if self.mode == "research" else "offline_test",
            "verification_kind": "live_network" if self.mode == "research" and len(self.client.responses) > start else "offline_test" if self.mode == "offline_test" else "not_requested",
            "upstream_grant_status": self.client.permission.get("upstream_grant_status", "unconfirmed"),
            "evidence": deepcopy(self.client.responses[start:]), **values}

    def fetch_catalog(self, *, taxonomy=TAXONOMY):
        if taxonomy != TAXONOMY:
            raise ValueError("only_confirmed_sina_industry_taxonomy_supported")
        start, rows, issues = len(self.client.responses), [], []
        try:
            rows = parse_catalog(self.client.get(CATALOG_URL, operation="sector_catalog"))
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            issues.append(str(exc))
        return self._packet("catalog", start, rows, issues, boundary=bool(rows) and not issues,
            boundary_basis="complete_single_assignment_document", source_row_limit=None,
            summary_date_verified=False, date_basis="current_observation_not_historical_membership")

    def fetch_members(self, sector):
        if not isinstance(sector, dict) or sector.get("taxonomy") != TAXONOMY:
            raise ValueError("confirmed_catalog_sector_required")
        node = sector.get("provider_id")
        if not isinstance(node, str) or sector.get("sector_id") != "sina:" + node:
            raise ValueError("sector_identity_mismatch")
        start, rows, issues, pages = len(self.client.responses), [], [], []
        before, after, boundary = None, None, False
        seen, previous = set(), None
        try:
            before = _source_count(self.client.get(count_url(node), operation="member_count_before"))
            for page in range(1, 101):
                response = self.client.get(members_url(node, page), operation="sector_members")
                values = parse_members(response)
                pages.append({"page": page, "row_count": len(values), "body_sha256": response.get("body_sha256"),
                    "evidence_path": response.get("evidence_path"), "explicit_terminal_empty_list": not values})
                if not values:
                    boundary = True
                    break
                page_invalid = False
                for row in values:
                    symbol = row.get("symbol")
                    if symbol in seen:
                        issues.append("duplicate_member_across_pages")
                        page_invalid = True
                    if isinstance(symbol, str) and previous is not None and symbol <= previous:
                        issues.append("membership_symbol_sort_unstable")
                        page_invalid = True
                    if isinstance(symbol, str):
                        seen.add(symbol)
                        previous = symbol
                    issues.extend(row["issues"])
                rows.extend(values)
                if page_invalid:
                    break
            if not boundary:
                issues.append("membership_terminal_boundary_unverified")
            if boundary:
                after = _source_count(self.client.get(count_url(node), operation="member_count_after"))
                if before != after:
                    issues.append("membership_changed_during_collection")
                if after != len(rows):
                    issues.append("membership_count_endpoint_discrepancy_unresolved")
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            issues.append(str(exc))
        displayed = sector.get("displayed_count")
        return self._packet("members", start, rows, issues, boundary=boundary, sector_id=sector["sector_id"],
            pages=pages, count_before=before, count_after=after, displayed_count=displayed,
            count_discrepancy={"catalog": displayed, "count_endpoint_before": before,
                "count_endpoint_after": after, "returned": len(rows),
                "catalog_differs": displayed is not None and displayed != len(rows),
                "count_endpoint_differs": after is not None and after != len(rows)},
            date_basis="current_observation_not_historical_membership")

    def fetch_quotes(self, identities, target_date):
        iso_date(target_date)
        known, seen = [], set()
        for value in identities:
            identity = _identity(value)
            symbol = _quote_symbol(identity)
            if symbol in seen:
                raise ValueError("duplicate_or_conflicting_quote_identity")
            known.append(identity)
            seen.add(symbol)
        start, rows, issues, missing, batches = len(self.client.responses), [], [], [], []
        if not known:
            issues.append("quote_identity_set_empty")
        for begin in range(0, len(known), 80):
            batch = known[begin:begin+80]
            url = QUOTE_BASE + ",".join(_quote_symbol(item) for item in batch)
            try:
                response = self.client.get(url, operation="dated_quote_snapshot")
                values, absent = parse_quotes(response, batch, target_date)
                rows.extend(values)
                missing.extend(absent)
                batches.append({"offset_in_local_request": begin, "requested_count": len(batch),
                    "returned_count": len(values), "missing_symbols": absent,
                    "evidence_path": response.get("evidence_path")})
                if absent:
                    issues.append("quote_response_missing_requested_symbols")
                if any(row["issues"] for row in values):
                    issues.append("quote_field_or_date_quality_failed")
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                issues.append(str(exc))
                missing.extend(_quote_symbol(item) for item in batch)
            if self.client.stopped or self.client.calls >= self.client.max_requests:
                missing.extend(_quote_symbol(item) for item in known[begin+80:])
                issues.append("quote_source_stopped_before_completion")
                break
        return self._packet("quotes", start, rows, issues, boundary=not missing and len(rows) == len(known),
            target_date=target_date, expected_count=len(known), missing_symbols=sorted(set(missing)), batches=batches,
            quote_complete=not issues and bool(known), status_complete=False, research_ready=False,
            source_business_date=target_date if rows and not issues else None,
            data_kind="dated_lightweight_quote_observation_not_final_daily_bar")


if __name__ == "__main__":
    if sys.argv[1:] != ["--http-worker"]:
        raise SystemExit("module only supports --http-worker")
    _http_worker()
