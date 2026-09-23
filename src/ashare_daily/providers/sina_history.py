"""Sina whole-history facts and observed factor tables for a frozen selection.

The response JavaScript is parsed as an inert assignment. Only the pinned local
MIT decoder runs in a bounded Node VM. Public availability is not a data grant.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

from .base import DailyBarRequest, MarketDataProvider, ProviderResult, iso_date
from .baostock import SHANGHAI
from .sina_decoder import DECODER_JS, DECODER_SHA256

SCHEMA = "sina-history-response-v1"
QUALITY_VERSION = "sina-history-quality-v1"
MAX_BODY = 2_000_000
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _constant(value):
    raise ValueError("nonfinite JSON number: " + value)


JSON = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant)


def _comments(text):
    while True:
        text = text.lstrip()
        if not text.startswith("/*"):
            return text
        end = text.find("*/", 2)
        if end < 0:
            raise ValueError("unclosed response comment")
        text = text[end + 2:]


def parse_assignment(text, expected_name, *, parenthesized=False):
    text = _comments(text)
    match = re.match(r"var\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*", text)
    if not match or match[1] != expected_name:
        raise ValueError("source assignment identity mismatch")
    rest = text[match.end():]
    if parenthesized:
        if not rest.startswith("("):
            raise ValueError("JSONP opening boundary missing")
        rest = rest[1:].lstrip()
    value, end = JSON.raw_decode(rest)
    tail = rest[end:].lstrip()
    if parenthesized:
        if not tail.startswith(")"):
            raise ValueError("JSONP closing boundary missing")
        tail = tail[1:].lstrip()
    if tail.startswith(";"):
        tail = tail[1:]
    if _comments(tail):
        raise ValueError("trailing executable content rejected")
    return value


def source_symbol(identity):
    return {"SSE": "sh", "SZSE": "sz"}[identity.exchange] + identity.code


def history_url(identity, observed_date):
    symbol = source_symbol(identity)
    stamp = iso_date(observed_date).strftime("%Y_%m_%d")
    if identity.board == "star":
        return f"https://quotes.sina.cn/cn/api/jsonp.php/var%20_{symbol}{stamp}=/KC_MarketDataService.getKLineData?symbol={symbol}"
    return f"https://finance.sina.com.cn/realstock/company/{symbol}/hisdata_klc2/klc_kl.js"


def factor_url(identity):
    return f"https://finance.sina.com.cn/realstock/company/{source_symbol(identity)}/qfq.js"


def _timestamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None or stamp > datetime.now(SHANGHAI):
        raise ValueError("invalid or future observation timestamp")
    return stamp


def _http_bytes(http, endpoint, mode):
    provenance = "online" if mode == "research" else "offline_test"
    kind = "live_network" if mode == "research" else "offline_test"
    if (http.get("url") != endpoint or http.get("status") != "received" or
            type(http.get("http_status")) is not int or http["http_status"] != 200 or
            http.get("body_complete") is not True or http.get("verification_kind") != kind or
            http.get("provenance_mode", provenance) != provenance):
        raise ValueError("complete HTTP response and matching provenance required")
    _timestamp(http["fetched_at"])
    encoded = http.get("body_base64")
    if not isinstance(encoded, str) or len(encoded) > MAX_BODY * 4 // 3 + 4:
        raise ValueError("raw HTTP body bound")
    body = base64.b64decode(encoded, validate=True)
    if not body or len(body) > MAX_BODY or hashlib.sha256(body).hexdigest() != http.get("body_sha256"):
        raise ValueError("raw HTTP body hash/size mismatch")
    if http.get("body_bytes") != len(body):
        raise ValueError("raw HTTP byte count mismatch")
    return body


def _runtime_path(relative):
    path = _PROJECT_ROOT / relative
    if any(part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()) for part in (path, *path.parents)):
        raise ValueError("local Node runtime path must not traverse a symlink or junction")
    if not path.resolve().is_relative_to(_PROJECT_ROOT):
        raise ValueError("local Node runtime path must remain inside the project")
    return path


def _node_identity(path):
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or not 0 < details.st_size <= 200_000_000 or not os.access(path, os.X_OK):
        raise ValueError("local Node runtime must be a regular executable file")
    return (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_ctime_ns)


@lru_cache(maxsize=8)
def _verified_node_binary(path_text, expected_hash, identity):
    """Hash once per unchanged installed binary, never download or execute it."""
    path = Path(path_text)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_hash:
        raise ValueError("local Node runtime executable SHA256 mismatch")
    if _node_identity(path) != identity:
        raise ValueError("local Node runtime changed during verification")
    return path_text


def _node_runtime():
    """Resolve the pinned Linux project runtime without relying on systemd PATH."""
    if sys.platform == "linux":
        manifest = _runtime_path("config/node_runtime.json")
        if manifest.exists():
            if not manifest.is_file() or manifest.stat().st_size > 16_384:
                raise ValueError("local Node runtime manifest is not a bounded JSON file")
            try:
                config = JSON.decode(manifest.read_text(encoding="utf-8-sig"))
            except (ValueError, OSError) as exc:
                raise ValueError("local Node runtime manifest cannot be read") from exc
            if (not isinstance(config, dict) or config.get("schema_version") != "node-runtime-v1"
                    or config.get("platform") != "linux-x64" or config.get("executable") != ".tools/node/bin/node"
                    or not isinstance(config.get("version"), str) or not re.fullmatch(r"v\d+\.\d+\.\d+", config["version"])
                    or not isinstance(config.get("executable_sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", config["executable_sha256"])):
                raise ValueError("local Node runtime manifest contract mismatch")
            executable = _runtime_path(config["executable"])
            if executable.exists():
                return _verified_node_binary(str(executable), config["executable_sha256"], _node_identity(executable))
    node = shutil.which("node")
    if node:
        return node
    raise ValueError("local Node runtime unavailable: install the verified project .tools/node/bin/node or provide Node on PATH; source access did not fail")


def decode_pinned(encoded):
    if (not isinstance(encoded, str) or len(encoded) > MAX_BODY or
            not re.fullmatch(r"[A-Za-z0-9+/]+", encoded)):
        raise ValueError("invalid compressed history alphabet")
    if hashlib.sha256(DECODER_JS.encode()).hexdigest() != DECODER_SHA256:
        raise ValueError("pinned decoder hash mismatch")
    node = _node_runtime()
    # Only this locally pinned decoder becomes code; source bytes are VM data.
    wrapper = ("const fs=require('node:fs'),vm=require('node:vm');"
        "const p=JSON.parse(fs.readFileSync(0,'utf8'));"
        "const c=vm.createContext({encoded:p.encoded},{codeGeneration:{strings:false,wasm:false}});"
        "new vm.Script(p.decoder).runInContext(c,{timeout:1000});"
        "const out=new vm.Script('JSON.stringify(d(encoded))').runInContext(c,{timeout:1000});"
        "if(typeof out!=='string'||out.length>10000000)throw Error('Output bound');process.stdout.write(out);")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = subprocess.run([node, "--max-old-space-size=96", "-e", wrapper],
        input=json.dumps({"encoded": encoded, "decoder": DECODER_JS}), capture_output=True, text=True, encoding="utf-8", timeout=5,
        env={**os.environ, "TZ": "Asia/Shanghai"}, **options)
    if result.returncode != 0:
        raise ValueError("bounded pinned decoder failed")
    return JSON.decode(result.stdout)


def _history_rows(http, request, mode):
    stamp = _timestamp(http["fetched_at"]).astimezone(SHANGHAI).date().isoformat()
    url = history_url(request.identity, stamp)
    text = _http_bytes(http, url, mode).decode("utf-8-sig")
    symbol = source_symbol(request.identity)
    if request.identity.board == "star":
        rows = parse_assignment(text, "_" + symbol + stamp.replace("-", "_"), parenthesized=True)
    else:
        rows = decode_pinned(parse_assignment(text, "KLC_K2_" + symbol))
    if not isinstance(rows, list) or not rows or len(rows) > 20000 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid decoded history list")
    return rows


def _number(value):
    if value in (None, "", "--", "-"):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 64:
        raise ValueError("invalid number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid number") from exc
    if not number.is_finite() or (number and not -30 <= number.adjusted() <= 30):
        raise ValueError("nonfinite or out-of-range number")
    return number


def _text(value):
    return None if value is None else format(value, "f")


def _date(value):
    if not isinstance(value, str):
        raise ValueError("missing source date")
    if len(value) == 10:
        return iso_date(value).isoformat()
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("ambiguous source date")
    return stamp.astimezone(SHANGHAI).date().isoformat()


def parse_factors(http, identity, mode):
    value = parse_assignment(_http_bytes(http, factor_url(identity), mode).decode("utf-8-sig"), source_symbol(identity) + "qfq")
    if not isinstance(value, dict) or set(value) != {"total", "data"} or not isinstance(value["data"], list):
        raise ValueError("factor envelope schema changed")
    total = value["total"]
    if isinstance(total, bool) or not isinstance(total, (str, int)) or not str(total).isdigit() or int(total) != len(value["data"]) or not 0 < len(value["data"]) <= 10000:
        raise ValueError("factor total/end boundary mismatch")
    observed = _timestamp(http["fetched_at"]).astimezone(SHANGHAI).date().isoformat()
    seen, rows = set(), []
    for row in value["data"]:
        if not isinstance(row, dict) or set(row) != {"d", "f"}:
            raise ValueError("factor fields changed")
        day, factor = _date(row["d"]), _number(row["f"])
        if day > observed or day in seen or factor is None or factor <= 0:
            raise ValueError("invalid, duplicate or future factor")
        seen.add(day)
        rows.append({"date": day, "factor": _text(factor)})
    return sorted(rows, key=lambda row: row["date"])


def response_hash(raw, factors=None):
    return _hash({"raw": raw["body_sha256"], "factors": factors["body_sha256"] if factors else None})


def normalize_sina_response(response, request, *, mode):
    if mode not in {"research", "offline_test"}:
        raise ValueError("invalid provenance mode")
    identity, adjusted = request.identity, request.adjustment_mode == "forward_adjusted"
    if request.adjustment_mode not in {"unadjusted", "forward_adjusted"}:
        raise ValueError("unsupported adjustment mode")
    required = {"schema_version": SCHEMA, "provider": "sina", "operation": "daily_bars",
        "identity": asdict(identity), "parameters": request.parameters(), "expected_dates": list(request.expected_dates),
        "source_symbol": source_symbol(identity), "source_business_date": None, "ok": True, "error_code": "0",
        "provenance_mode": "online" if mode == "research" else "offline_test",
        "verification_kind": "live_network" if mode == "research" else "offline_test"}
    if any(response.get(key) != value for key, value in required.items()):
        raise ValueError("Sina response identity/request/provenance mismatch")
    observed = _timestamp(response["fetched_at"])
    if iso_date(request.end_date) > observed.astimezone(SHANGHAI).date():
        raise ValueError("requested date exceeds observation")
    raw, factor_http = response["raw_history"], response.get("factor_table")
    if bool(factor_http) != adjusted or response["raw_hash"] != response_hash(raw, factor_http):
        raise ValueError("Sina raw/factor version hash mismatch")
    times = [_timestamp(raw["fetched_at"])] + ([_timestamp(factor_http["fetched_at"])] if factor_http else [])
    if observed != max(times):
        raise ValueError("Sina component observation time mismatch")
    if response.get("source_endpoint") != raw.get("url"):
        raise ValueError("source endpoint mismatch")
    source_rows = _history_rows(raw, request, mode)
    factors = parse_factors(factor_http, identity, mode) if adjusted else []
    records, issues, seen, all_dates, raw_fact_hashes = [], [], set(), [], {}
    raw_observed = _timestamp(raw["fetched_at"]).astimezone(SHANGHAI).date().isoformat()
    star = identity.board == "star"
    mapping = {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v", "postVol": "pv", "postAmt": "pa"} if star else {}
    factor_missing = []
    for index, row in enumerate(source_rows):
        day = None
        try:
            day = _date(row.get("d" if star else "date"))
            if day in seen or day > raw_observed:
                raise ValueError("duplicate or future source date")
            seen.add(day)
            all_dates.append(day)
            if day not in request.expected_dates:
                continue
            values = {key: _number(row.get(mapping.get(key, key))) for key in
                ("open", "high", "low", "close", "volume", "amount", "prevclose", "postVol", "postAmt")}
            if star:
                values["amount"] = None
            prices = [values[key] for key in ("open", "high", "low", "close")]
            if any(value is not None and value <= 0 for value in prices):
                raise ValueError("nonpositive price")
            if all(value is not None for value in prices) and (values["low"] > min(prices) or values["high"] < max(prices)):
                raise ValueError("invalid OHLC relationship")
            if any(values[key] is not None and values[key] < 0 for key in ("volume", "amount", "postVol", "postAmt")):
                raise ValueError("negative volume/amount")
            if values["volume"] is not None and values["volume"] != values["volume"].to_integral_value():
                raise ValueError("volume is not integral shares")
            if values["prevclose"] is not None and values["prevclose"] <= 0:
                raise ValueError("nonpositive previous close")
            if values["volume"] is not None and values["amount"] is not None and ((values["volume"] == 0 and values["amount"] > 0) or (values["amount"] == 0 and values["volume"] > 0)):
                raise ValueError("volume/amount inconsistency")
            raw_values = deepcopy(values)
            chosen = None
            if adjusted:
                eligible = [factor for factor in factors if factor["date"] <= day]
                if not eligible:
                    factor_missing.append(day)
                    raise ValueError("factor missing for requested date")
                chosen = eligible[-1]
                with localcontext() as context:
                    context.prec = 40
                    for key in ("open", "high", "low", "close"):
                        if values[key] is not None:
                            values[key] /= Decimal(chosen["factor"])
                values["prevclose"] = None
            flags = ["missing_" + key for key in ("open", "high", "low", "close") if values[key] is None]
            flags += [name for key, name in (("volume", "missing_volume_shares"), ("amount", "missing_amount_cny"), ("prevclose", "missing_preclose")) if values[key] is None]
            record = {"security_id": identity.security_id, "provider": "sina", "symbol": identity.symbol,
                "trade_date": day, "adjustment_mode": request.adjustment_mode,
                **{key: _text(values[key]) for key in ("open", "high", "low", "close")},
                "preclose": _text(values["prevclose"]), "volume_shares": int(values["volume"]) if values["volume"] is not None else None,
                "amount_cny": _text(values["amount"]), "tradestatus": None, "is_st": None,
                "turnover_ratio": None, "provider_change_ratio": None, "reference_change_ratio": None,
                "price_unit": "CNY", "volume_unit": "shares", "amount_unit": "CNY",
                "after_hours_volume_inclusion": "unverified_no_addition", "quality_flags": flags,
                "source_units": {"price": "CNY", "volume": "shares", "amount": "unavailable" if star else "CNY"},
                "unit_evidence": "pinned_akshare_sina_contract_not_independent_reconciliation"}
            raw_record = deepcopy(record)
            raw_record["adjustment_mode"] = "unadjusted"
            raw_record.update({key: _text(raw_values[key]) for key in ("open", "high", "low", "close")})
            raw_record["preclose"] = _text(raw_values["prevclose"])
            if raw_values["prevclose"] is not None:
                raw_record["quality_flags"] = [flag for flag in raw_record["quality_flags"] if flag != "missing_preclose"]
            raw_fact_hashes[day] = _hash(raw_record)
            if chosen:
                record.update(adjustment_factor=chosen["factor"], adjustment_factor_date=chosen["date"])
            records.append(record)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append({"row_index": index, "date": day, "reason": str(exc)})
    if all_dates != sorted(all_dates):
        issues.append({"row_index": None, "date": None, "reason": "source_dates_not_ordered"})
    records.sort(key=lambda row: row["trade_date"])
    missing = sorted(set(request.expected_dates) - {row["trade_date"] for row in records})
    covered = not missing and not issues
    price_ready = covered and all(all(row[key] is not None for key in ("open", "high", "low", "close")) for row in records)
    numeric_ready = price_ready and all(row["volume_shares"] is not None and row["amount_cny"] is not None for row in records)
    dates = [row["trade_date"] for row in records]
    return {"quality_rules_version": QUALITY_VERSION, "provider": "sina", "records": records,
        "expected_dates": list(request.expected_dates), "raw_row_count": len(source_rows),
        "physical_response_scope": "single_security_available_whole_history", "source_row_limit": None,
        "source_first_date": min(all_dates) if all_dates else None, "source_latest_date": max(all_dates) if all_dates else None,
        "missing_dates": missing, "quality_issues": issues, "quality_issue_dates": [row["trade_date"] for row in records if row["quality_flags"]],
        "trading_status_unknown_dates": dates, "status_unknown_dates": dates, "valid_quote_dates": [], "suspended_dates": [],
        "quote_complete": False, "status_complete": False, "complete": False, "research_ready": False,
        "calendar_coverage_complete": covered, "numeric_price_complete": price_ready, "numeric_history_complete": numeric_ready,
        "adjustment_window_complete": bool(adjusted and price_ready and not factor_missing),
        "factor_source_total": len(factors) if adjusted else None, "factor_missing_dates": factor_missing,
        "adjustment_anchor_hash": response["raw_hash"] if adjusted else None,
        "raw_component_hash": raw["body_sha256"], "raw_fact_hashes": raw_fact_hashes,
        "factor_component_hash": factor_http["body_sha256"] if adjusted else None,
        "source_business_date": None, "units_independently_verified": False,
        "known_gaps": ["risk_states_unknown", "previous_close_may_be_missing", "after_hours_inclusion_unverified"] + (["star_amount_unavailable"] if star else [])}


class SinaHistoryProvider(MarketDataProvider):
    name = "sina"
    capabilities = frozenset({"daily_bars"})

    def __init__(self, output_directory, *, permission, mode="research", timeout_seconds=15,
                 pause_seconds=2, max_requests=2000, transport=None):
        if mode not in {"research", "offline_test"} or (mode == "research" and transport is not None):
            raise ValueError("research rejects injected history transports")
        if mode == "offline_test" and "research" in {part.casefold() for part in Path(output_directory).parts}:
            raise ValueError("offline history evidence must not enter research paths")
        self.mode, self.permission, self._raw = mode, deepcopy(permission), {}
        from .sina_sectors import SinaHttpClient
        self.client = SinaHttpClient(Path(output_directory), mode=mode, transport=transport, permission=permission,
            timeout_seconds=timeout_seconds, pause_seconds=pause_seconds, max_requests=max_requests)

    def seed_raw(self, request, archived_response):
        raw_request = DailyBarRequest(request.identity, request.start_date, request.end_date, request.expected_dates, "unadjusted")
        response = deepcopy(archived_response)
        response.update(parameters=raw_request.parameters(), expected_dates=list(raw_request.expected_dates))
        quality = normalize_sina_response(response, raw_request, mode=self.mode)
        if quality["calendar_coverage_complete"]:
            self._raw[request.identity.security_id] = response["raw_history"]
            return True
        return False

    def release_security(self, security_id):
        """Drop only the completed security's in-memory whole-history body."""
        self._raw.pop(security_id, None)

    def fetch_daily_bars(self, request):
        identity = request.identity
        response = {"schema_version": SCHEMA, "provider": "sina", "operation": "daily_bars",
            "identity": asdict(identity), "parameters": request.parameters(), "expected_dates": list(request.expected_dates),
            "source_symbol": source_symbol(identity), "source_business_date": None,
            "provenance_mode": "online" if self.mode == "research" else "offline_test",
            "verification_kind": "live_network" if self.mode == "research" else "offline_test",
            "ok": False, "status": "not_requested", "error_code": "not_requested", "fetched_at": datetime.now(SHANGHAI).isoformat(),
            "raw_history": None, "factor_table": None, "raw_hash": None}
        metrics = {"requests": 0, "network_requests": 0, "retries": 0, "response_bytes": 0, "physical_source_rows": 0, "attempts": []}
        quality = {}
        if request.adjustment_mode not in {"unadjusted", "forward_adjusted"}:
            response.update(status="unsupported", error_code="unsupported_adjustment")
        else:
            raw = self._raw.get(identity.security_id)
            if raw is None:
                raw = self.client.get(history_url(identity, datetime.now(SHANGHAI).date().isoformat()), operation="raw_daily")
                self._metrics(metrics, raw)
            response.update(raw_history=raw, source_endpoint=raw.get("url"), fetched_at=raw["fetched_at"])
            if raw.get("status") == "received":
                self._raw[identity.security_id] = raw
                if request.adjustment_mode == "forward_adjusted":
                    factors = self.client.get(factor_url(identity), operation="qfq_factor")
                    self._metrics(metrics, factors)
                    response["factor_table"] = factors
                    response["fetched_at"] = max(raw["fetched_at"], factors["fetched_at"])
                failed = next((item for item in (raw, response["factor_table"]) if item is not None and item.get("status") != "received"), None)
                if failed:
                    response.update(status=failed["status"], error_code=failed.get("error_code", failed["status"]))
                else:
                    response.update(ok=True, status="partial", error_code="0", raw_hash=response_hash(raw, response["factor_table"]))
                    try:
                        quality = normalize_sina_response(response, request, mode=self.mode)
                        metrics["physical_source_rows"] = quality["raw_row_count"] if any(a.get("url") == raw.get("url") for a in metrics["attempts"]) else 0
                    except (ValueError, KeyError, TypeError, UnicodeError, subprocess.TimeoutExpired) as exc:
                        response.update(ok=False, status="schema_changed", error_code="source_schema_error", error_msg=str(exc))
            else:
                response.update(status=raw.get("status", "unavailable"), error_code=raw.get("error_code", raw.get("status")))
        return ProviderResult(self.name, "daily_bars", response["status"], response,
            records=quality.get("records", []), quality=quality, source_symbol=source_symbol(identity),
            source_endpoint=response.get("source_endpoint"), source_business_date=None, fetched_at=response["fetched_at"], metrics=metrics)

    @staticmethod
    def _metrics(metrics, http):
        items = http.get("metrics", {})
        metrics["requests"] += items.get("requests", http.get("attempts", 1))
        metrics["network_requests"] += items.get("network_requests", int(http.get("verification_kind") == "live_network") * items.get("requests", http.get("attempts", 1)))
        metrics["retries"] += items.get("retries", http.get("retries", 0))
        metrics["response_bytes"] += http.get("body_bytes", 0)
        metrics["attempts"].append({"url": http.get("url"), "status": http.get("status"),
            "body_sha256": http.get("body_sha256"), "evidence_path": http.get("evidence_path")})

    def close(self):
        try:
            self.client.close()
        finally:
            self._raw.clear()
