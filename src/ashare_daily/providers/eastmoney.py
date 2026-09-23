"""Permission-gated EastMoney website adapter; it is not an authorized public API.

Field evidence is pinned AKShare source, not a grant to acquire/store the data.
The daily endpoint does not evidence ST, trading status or previous close. These
remain unknown and cannot pass the existing quote/research quality gate.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo

from .base import DailyBarRequest, MarketDataProvider, ProviderResult, QuoteResult, QuoteSnapshot, SecurityIdentity, iso_date


HISTORY_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
QUOTE_ENDPOINT = "https://push2.eastmoney.com/api/qt/stock/get"
SCHEMA_VERSION = "f2-eastmoney-response-v1"
QUALITY_RULES_VERSION = "f2-eastmoney-quality-v1"
AKSHARE_COMMIT = "8e95744b79ae22326308ccd2b4e62650c5b53c55"
SHANGHAI = ZoneInfo("Asia/Shanghai")
ADJUSTMENTS = {"unadjusted": "0", "forward_adjusted": "1", "backward_adjusted": "2"}
# Public client identifier in the referenced open-source client; not credentials
# or evidence of permission to use the underlying data service.
PUBLIC_CLIENT_ID = "7eea3edcaed734bea9cbfc24409ed989"


def history_parameters(request: DailyBarRequest) -> dict[str, str]:
    return {"fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": PUBLIC_CLIENT_ID, "klt": "101", "fqt": ADJUSTMENTS[request.adjustment_mode],
            "secid": request.identity.source_symbol("eastmoney"),
            "beg": request.start_date.replace("-", ""), "end": request.end_date.replace("-", "")}


def quote_parameters(identity: SecurityIdentity) -> dict[str, str]:
    # fltt=2 returns the decimal display prices in the pinned client; do not
    # divide them again by 10**f59. f86 is retained raw, not guessed as a date.
    return {"fltt": "2", "invt": "2", "secid": identity.source_symbol("eastmoney"),
            "fields": "f43,f44,f45,f46,f60,f47,f48,f57,f58,f86,f59"}


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timezone-aware fetched_at required")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid fetched_at") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timezone-aware fetched_at required")
    if stamp.astimezone(SHANGHAI) > _now():
        raise ValueError("future fetched_at is not an observed response")
    return stamp


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _strict_json(raw: bytes) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite JSON number")

    try:
        parsed = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds parser bound") from exc
    if not isinstance(parsed, dict):
        raise ValueError("JSON object required")
    return parsed


def _validated_http(response: dict, *, endpoint: str, parameters: dict, mode: str) -> dict:
    if mode not in {"research", "offline_test"}:
        raise ValueError("unsupported provenance mode")
    provenance = "online" if mode == "research" else "offline_test"
    kind = "live_network" if mode == "research" else "offline_test"
    http = response.get("http")
    if not isinstance(http, dict) or http.get("schema_version") != "bounded-http-response-v1":
        raise ValueError("HTTP evidence schema mismatch")
    if http.get("request") != {"url": endpoint, "parameters": parameters}:
        raise ValueError("HTTP endpoint/parameters mismatch")
    if (http.get("provenance_mode") != provenance or http.get("verification_kind") != kind
            or response.get("provenance_mode") != provenance or response.get("verification_kind") != kind):
        raise ValueError("HTTP provenance mode mismatch")
    if http.get("ok") is not True or http.get("error_code") != "0" or type(http.get("http_status")) is not int or http["http_status"] != 200 or http.get("body_complete") is not True:
        raise ValueError("HTTP success and complete body required")
    if not isinstance(http.get("body_base64"), str) or len(http["body_base64"]) > 2_800_000:
        raise ValueError("invalid or oversized raw HTTP body")
    try:
        raw = base64.b64decode(http["body_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid raw HTTP encoding") from exc
    if not raw or len(raw) > 2_000_000:
        raise ValueError("empty or oversized raw HTTP body")
    signature = hashlib.sha256(raw).hexdigest()
    if http.get("body_sha256") != signature or response.get("raw_hash") != signature:
        raise ValueError("raw HTTP hash mismatch")
    payload = _strict_json(raw)
    # Strict serialization also rejects bool/int or int/float equality tricks.
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    if canonical(payload) != canonical(response.get("payload")):
        raise ValueError("parsed HTTP payload mismatch")
    if http.get("fetched_at") != response.get("fetched_at"):
        raise ValueError("HTTP observation timestamp mismatch")
    _timestamp(response.get("fetched_at"))
    if type(payload.get("rc")) is not int or payload["rc"] != 0:
        raise ValueError("upstream response rc is not zero")
    return payload


def _number(value: Any, name: str, *, signed: bool = False) -> Decimal | None:
    if value is None or value in ("", "-"):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("invalid_number:" + name)
    if len(str(value)) > 64:
        raise ValueError("invalid_number:" + name)
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid_number:" + name) from exc
    if not number.is_finite() or (not signed and number < 0) or (number and not -30 <= number.adjusted() <= 30):
        raise ValueError("invalid_number:" + name)
    return number


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _shift(value: Decimal | None, places: int) -> Decimal | None:
    if value is None:
        return None
    sign, digits, exponent = value.as_tuple()
    return Decimal((sign, digits, exponent + places))


def _volume_shares(lots: Decimal | None) -> int | None:
    shares = _shift(lots, 2)
    if shares is None:
        return None
    if shares != shares.to_integral_value():
        raise ValueError("volume_not_integral_shares")
    if shares > 2**63 - 1:
        raise ValueError("volume_exceeds_storage_integer_bound")
    return int(shares)


def _prices(prices: dict[str, Decimal | None]) -> None:
    if any(value is not None and value <= 0 for value in prices.values()):
        raise ValueError("price_must_be_positive")
    high, low = prices["high"], prices["low"]
    if high is not None and low is not None and high < low:
        raise ValueError("ohlc_range_invalid")
    if any(value is not None and ((high is not None and value > high) or (low is not None and value < low))
           for value in (prices["open"], prices["close"])):
        raise ValueError("ohlc_range_invalid")


def normalize_eastmoney_response(response: dict, request: DailyBarRequest, *, mode: str) -> dict:
    """Revalidate archived bytes and request binding; never trust saved quality.

There is no documented pagination/total or hard server row cap for this call.
Coverage is reconciled against the caller's independently verified calendar.
This is a single complete HTTP response, not proof of complete market coverage.
"""
    identity = request.identity
    expected_envelope = {"schema_version": SCHEMA_VERSION, "provider": "eastmoney", "operation": "daily_bars",
                         "identity": asdict(identity), "security_id": identity.security_id, "symbol": identity.symbol,
                         "scope": identity.scope, "parameters": request.parameters(), "expected_dates": list(request.expected_dates),
                         "source_endpoint": HISTORY_ENDPOINT, "source_symbol": identity.source_symbol("eastmoney"),
                         "source_business_date": None, "ok": True, "error_code": "0"}
    if any(key not in response or response[key] != value for key, value in expected_envelope.items()):
        raise ValueError("EastMoney response identity/request/schema mismatch")
    if response.get("ok") is not True or response.get("identity", {}).get("metadata_verified") is not True:
        raise ValueError("source-verified identity and successful response required")
    payload = _validated_http(response, endpoint=HISTORY_ENDPOINT, parameters=history_parameters(request), mode=mode)
    if iso_date(request.end_date) > _timestamp(response["fetched_at"]).astimezone(SHANGHAI).date():
        raise ValueError("requested history end date is after observation")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("code") != identity.code or not isinstance(data.get("klines"), list):
        raise ValueError("history data/code/klines schema mismatch or empty source data")
    rows = data["klines"]
    if len(rows) > 500:
        raise ValueError("history response exceeds bounded request contract")
    if "market" in data and (type(data["market"]) is not int or data["market"] != (1 if identity.exchange == "SSE" else 0)):
        raise ValueError("history market identity mismatch")
    dates = [row.split(",")[0] for row in rows if isinstance(row, str)]
    duplicates = {day for day in dates if dates.count(day) > 1}
    records, issues = [], []
    if dates != sorted(dates):
        issues.append({"row_index": None, "date": None, "reason": "source_dates_not_ordered"})
    for index, row in enumerate(rows):
        raw_day = row.split(",")[0] if isinstance(row, str) else None
        try:
            if not isinstance(row, str) or len(row.split(",")) != 11:
                raise ValueError("history_requires_eleven_source_fields")
            parts = row.split(",")
            day = iso_date(parts[0]).isoformat()
            if day in duplicates:
                raise ValueError("duplicate_date")
            if day not in request.expected_dates:
                raise ValueError("date_not_in_verified_requested_calendar")
            prices = {"open": _number(parts[1], "open"), "close": _number(parts[2], "close"),
                      "high": _number(parts[3], "high"), "low": _number(parts[4], "low"), "preclose": None}
            _prices(prices)
            lots, amount = _number(parts[5], "volume"), _number(parts[6], "amount")
            volume = _volume_shares(lots)
            _number(parts[7], "amplitude")
            pct = _number(parts[8], "pct_change", signed=True)
            _number(parts[9], "price_change", signed=True)
            turnover = _number(parts[10], "turnover")
            flags = ["missing_" + key for key, value in prices.items() if value is None]
            if volume is None:
                flags.append("missing_volume_shares")
            if amount is None:
                flags.append("missing_amount_cny")
            if (volume == 0 and amount is not None and amount > 0) or (amount == 0 and volume is not None and volume > 0):
                flags.append("volume_amount_inconsistent")
            records.append({"security_id": identity.security_id, "provider": "eastmoney", "symbol": identity.symbol,
                            "trade_date": day, "adjustment_mode": request.adjustment_mode,
                            **{key: _text(value) for key, value in prices.items()},
                            "volume_shares": int(volume) if volume is not None else None, "amount_cny": _text(amount),
                            "tradestatus": None, "is_st": None,
                            "turnover_ratio": _text(_shift(turnover, -2)),
                            "provider_change_ratio": _text(_shift(pct, -2)),
                            "reference_change_ratio": None, "price_unit": "CNY", "volume_unit": "shares", "amount_unit": "CNY",
                            "source_units": {"price": "CNY", "volume": "lots_of_100_shares", "amount": "CNY", "turn": "percent", "pctChg": "percent"},
                            "after_hours_volume_inclusion": "unverified_no_addition", "quality_flags": flags})
        except (ValueError, TypeError, KeyError) as exc:
            issues.append({"row_index": index, "date": raw_day, "reason": str(exc)})
    records.sort(key=lambda row: row["trade_date"])
    observed = {row["trade_date"] for row in records}
    missing = sorted(set(request.expected_dates) - observed)
    status_unknown = [row["trade_date"] for row in records]
    bad_dates = [row["trade_date"] for row in records if row["quality_flags"]]
    return {"quality_rules_version": QUALITY_RULES_VERSION, "provider": "eastmoney", "security_id": identity.security_id,
            "symbol": identity.symbol, "start_date": request.start_date, "end_date": request.end_date,
            "adjustment_mode": request.adjustment_mode, "expected_dates": list(request.expected_dates),
            "records": records, "raw_row_count": len(rows), "missing_dates": missing, "quality_issues": issues,
            "quality_issue_dates": bad_dates, "quote_quality_issue_dates": bad_dates, "review_flags": [],
            "suspended_dates": [], "status_unknown_dates": status_unknown, "trading_status_unknown_dates": status_unknown,
            "valid_quote_dates": [], "quote_complete": False, "status_complete": False, "complete": False,
            "research_ready": False, "calendar_coverage_complete": not missing and not issues,
            "source_response_complete": True, "source_row_limit": None,
            "adjustment_version": "single_provider_current_window" if request.adjustment_mode != "unadjusted" else "unadjusted",
            "source_business_date": None, "provenance_mode": response["provenance_mode"],
            "known_gaps": ["previous_close_not_provided", "trading_status_not_provided", "st_status_not_provided",
                           "source_business_date_not_provided", "source_hard_row_limit_undocumented"]}


class EastMoneyProvider(MarketDataProvider):
    name = "eastmoney"
    capabilities = frozenset({"daily_bars", "quote"})

    def __init__(self, *, permission: dict | None = None, mode: str = "research", transport=None,
                 timeout_seconds: float = 15, max_attempts: int = 2, pause_seconds: float = .5):
        if mode not in {"research", "offline_test"}:
            raise ValueError("research or explicit offline_test mode required")
        if mode == "research" and transport is not None:
            raise ValueError("research mode rejects injected transports")
        self.permission, self.mode = deepcopy(permission or {}), mode
        if mode == "offline_test" and self.permission.get("enabled") is True and transport is None:
            raise ValueError("enabled offline_test requires explicit test transport")
        if transport is not None and not callable(transport) and not callable(getattr(transport, "get", None)):
            raise ValueError("transport must be callable or expose get")
        from .http import HttpClient
        # No worker/network starts during construction, including disabled mode.
        self._http = HttpClient(timeout_seconds=timeout_seconds, max_attempts=max_attempts, pause_seconds=pause_seconds)
        self._transport, self._stopped = transport, None

    def _permission_error(self) -> str | None:
        if self.permission.get("enabled") is not True:
            return "source_disabled"
        if self.permission.get("permission_status") != "approved":
            return "source_permission_unconfirmed"
        if not isinstance(self.permission.get("permission_basis"), str) or not self.permission["permission_basis"].strip():
            return "source_permission_basis_missing"
        if self.permission.get("purpose") != "personal_noncommercial_local_research":
            return "source_purpose_unconfirmed"
        if self.permission.get("permitted_storage") is not True or self.permission.get("permitted_automated_access") is not True:
            return "source_storage_or_access_unconfirmed"
        if self.permission.get("llm_export") is not False:
            return "source_export_boundary_unconfirmed"
        return None

    def _envelope(self, identity: SecurityIdentity, operation: str, parameters: dict, endpoint: str) -> dict:
        return {"schema_version": SCHEMA_VERSION, "provider": self.name, "operation": operation,
                "identity": asdict(identity), "security_id": identity.security_id, "symbol": identity.symbol,
                "scope": identity.scope, "parameters": parameters, "ok": False, "error_code": "not_requested", "error_msg": "",
                "fetched_at": _now().isoformat(), "source_endpoint": endpoint,
                "source_symbol": identity.source_symbol(self.name), "source_business_date": None,
                "provenance_mode": "online" if self.mode == "research" else "offline_test",
                "verification_kind": "not_requested",
                "network_sent": False, "raw_hash": None, "http": None, "payload": None}

    def _fetch(self, response: dict, parameters: dict) -> dict:
        denied = self._permission_error()
        if denied or self._stopped:
            response.update(status="permission_required" if denied else "circuit_open", error_code=denied or "source_stopped",
                            error_msg="数据源用途/程序访问/本地留存许可未确认或该客户端已停止；未发送请求。")
            return {"requests": 0, "retries": 0, "elapsed_seconds": 0, "network_sent": False,
                    "network_requests": 0, "mode": self.mode, "verification_kind": response["verification_kind"]}
        response["verification_kind"] = "live_network" if self.mode == "research" else "offline_test"
        if self.mode == "offline_test":
            transport = self._transport if callable(self._transport) else self._transport.get
            http = transport(response["source_endpoint"], deepcopy(parameters))
        else:
            http = self._http.get(response["source_endpoint"], parameters)
        if not isinstance(http, dict):
            raise ValueError("HTTP transport must return evidence object")
        response.update(http=deepcopy(http), raw_hash=http.get("body_sha256"), fetched_at=http.get("fetched_at"),
                        network_sent=self.mode == "research" and http.get("metrics", {}).get("requests", 0) > 0,
                        status=http.get("status", "schema_changed"), error_code=http.get("error_code", "http_schema_error"),
                        error_msg=http.get("error_msg", ""))
        metrics = deepcopy(http.get("metrics", {}))
        metrics.update(mode=self.mode, verification_kind=response["verification_kind"], network_sent=response["network_sent"],
                       network_requests=metrics.get("requests", 0) if self.mode == "research" else 0)
        if http.get("status") in {"permission_denied", "rate_limited", "circuit_open"}:
            self._stopped = http["status"]
        if http.get("ok") is True:
            try:
                response["payload"] = _strict_json(base64.b64decode(http["body_base64"], validate=True))
                _validated_http(response, endpoint=response["source_endpoint"], parameters=parameters, mode=self.mode)
                response.update(ok=True, error_code="0", error_msg="", status="received")
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                response.update(ok=False, status="schema_changed", error_code="response_schema_error", error_msg=str(exc))
        response["diagnostics"] = {"failure_stage": http.get("metrics", {}).get("failure_stage"),
                                   "events": http.get("metrics", {}).get("stages", [])}
        return metrics

    def fetch_daily_bars(self, request: DailyBarRequest) -> ProviderResult:
        response = self._envelope(request.identity, "daily_bars", request.parameters(), HISTORY_ENDPOINT)
        response["expected_dates"] = list(request.expected_dates)
        metrics = self._fetch(response, history_parameters(request))
        quality = {}
        if response["ok"]:
            try:
                quality = normalize_eastmoney_response(response, request, mode=self.mode)
                response["status"] = "partial"
            except (ValueError, TypeError, KeyError) as exc:
                response.update(ok=False, status="schema_changed", error_code="response_schema_error", error_msg=str(exc))
        return ProviderResult(self.name, "daily_bars", response["status"], response,
                              records=quality.get("records", []), quality=quality,
                              source_symbol=response["source_symbol"], source_endpoint=HISTORY_ENDPOINT,
                              source_business_date=None, fetched_at=response["fetched_at"], metrics=metrics)

    def fetch_quote(self, identity: SecurityIdentity, *, target_date: str) -> QuoteResult:
        iso_date(target_date)
        response = self._envelope(identity, "quote", {"code": identity.symbol, "target_date": target_date}, QUOTE_ENDPOINT)
        metrics = self._fetch(response, quote_parameters(identity))
        snapshot = None
        if response["ok"]:
            try:
                if iso_date(target_date) > _timestamp(response["fetched_at"]).astimezone(SHANGHAI).date():
                    raise ValueError("requested quote target date is after observation")
                data = response["payload"].get("data")
                if not isinstance(data, dict) or data.get("f57") != identity.code:
                    raise ValueError("quote code/schema mismatch")
                if not isinstance(data.get("f58"), str) or not data["f58"].strip():
                    raise ValueError("quote name/schema mismatch")
                prices = {name: _number(data.get(key), name) for name, key in
                          {"close": "f43", "open": "f46", "high": "f44", "low": "f45", "preclose": "f60"}.items()}
                _prices(prices)
                lots = _number(data.get("f47"), "volume")
                volume = _volume_shares(lots)
                flags = ["source_timestamp_unverified", "not_a_closing_daily_bar", "quote_amount_unit_unverified"]
                flags.extend("missing_" + key for key, value in prices.items() if value is None)
                if volume is None:
                    flags.append("missing_volume_shares")
                # f48 is named amount in the reviewed quote client, but its unit
                # is not specified there. Preserve raw instead of borrowing the
                # separate history endpoint's unit. f86 time semantics unknown.
                snapshot = QuoteSnapshot(identity.security_id, self.name, identity.symbol, identity.source_symbol(self.name),
                                         None, response["fetched_at"], _text(prices["close"]), _text(prices["preclose"]),
                                         _text(prices["open"]), _text(prices["high"]), _text(prices["low"]),
                                         int(volume) if volume is not None else None, None, data["f58"], flags)
                response["status"] = "partial"
            except (ValueError, TypeError, KeyError) as exc:
                response.update(ok=False, status="schema_changed", error_code="response_schema_error", error_msg=str(exc))
        return QuoteResult(self.name, response["status"], snapshot, response, metrics)

    def close(self) -> None:
        self._http.close()
