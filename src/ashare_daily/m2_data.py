"""M2 bounded, frozen BaoStock price windows, isolated from the M1 database.

A refresh always requests each complete dependency window once. It never joins
adjusted prices from different responses, never updates old bundles, and does
not claim that BaoStock supplies an atomic server-wide adjustment version.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from ashare_daily.providers.baostock import (
    BaoStockClient, SHANGHAI, expected_fields, raw_hash, validate_request,
)

MAX_SAMPLE_SIZE = 10
MAX_TRADING_DATES = 260


class AdjustedDataError(ValueError):
    """An adjusted response fails its identity, units, or sequence contract."""


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _write_new(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def _number(value: str | None, field: str, *, positive: bool = False, integer: bool = False) -> str | int | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise AdjustedDataError(f"{field} must be an original numeric string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise AdjustedDataError(f"invalid {field}") from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise AdjustedDataError(f"invalid {field}: finite {'positive' if positive else 'nonnegative'} value required")
    if integer and number != number.to_integral_value():
        raise AdjustedDataError(f"{field} must be an integer number of shares")
    return int(number) if integer else str(number)


def _state(value: str | None, field: str) -> bool | None:
    if value is None or value == "":
        return None
    if value not in ("0", "1"):
        raise AdjustedDataError(f"invalid {field}: expected supplier 0/1")
    return value == "1"


def normalize_adjusted_response(
    response: dict[str, Any], *, symbol: str, security_type: str,
    trading_dates: list[date],
) -> dict[str, Any]:
    """Validate one whole response and retain NULLs and explicit missing dates."""
    parameters = validate_request("history", {
        "code": symbol, "security_type": security_type,
        "start_date": trading_dates[0].isoformat(), "end_date": trading_dates[-1].isoformat(),
        "adjustment_mode": "forward_adjusted" if security_type == "stock" else "unadjusted",
    })
    if response.get("ok") is not True or response.get("error_code") != "0":
        raise AdjustedDataError("response is not a successful provider result")
    if response.get("parameters") != parameters:
        raise AdjustedDataError("response request parameters do not match the frozen dependency window")
    fields, rows = response.get("fields"), response.get("rows")
    if not isinstance(fields, list) or len(set(fields)) != len(fields) or set(fields) != set(expected_fields("history", parameters)):
        raise AdjustedDataError("response fields do not match the stock/index contract")
    if not isinstance(rows, list) or response.get("raw_hash") != raw_hash(fields, rows):
        raise AdjustedDataError("response raw hash mismatch")
    try:
        fetched_at = datetime.fromisoformat(response["fetched_at"])
        if fetched_at.utcoffset() is None or fetched_at.utcoffset().total_seconds() != 28800:
            raise ValueError("not Asia/Shanghai offset")
    except (KeyError, TypeError, ValueError) as exc:
        raise AdjustedDataError("response fetched_at must be timezone-aware Asia/Shanghai") from exc
    dates = set(trading_dates)
    seen: set[date] = set()
    bars: list[dict[str, Any]] = []
    issues: list[str] = []
    is_stock = security_type == "stock"
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(fields) or any(not isinstance(item, str) for item in row.values()):
            raise AdjustedDataError("response row must retain all original string fields")
        if row["code"] != symbol:
            raise AdjustedDataError("response symbol mismatch")
        try:
            day = date.fromisoformat(row["date"])
            if day.isoformat() != row["date"]:
                raise ValueError("date format")
        except ValueError as exc:
            raise AdjustedDataError("invalid trade date") from exc
        if day not in dates:
            raise AdjustedDataError("response date outside requested trading calendar")
        if day in seen:
            raise AdjustedDataError("duplicate response trade date")
        seen.add(day)
        if is_stock and row["adjustflag"] != "2":
            raise AdjustedDataError("stock response is not forward-adjusted adjustflag=2")
        prices = {field: _number(row[field], field, positive=True)
                  for field in ("open", "high", "low", "close", "preclose")}
        low = Decimal(prices["low"]) if prices["low"] is not None else None
        high = Decimal(prices["high"]) if prices["high"] is not None else None
        if low is not None and high is not None and low > high:
            raise AdjustedDataError("inconsistent OHLC range")
        for field in ("open", "close"):
            if prices[field] is not None:
                value = Decimal(prices[field])
                if (low is not None and value < low) or (high is not None and value > high):
                    raise AdjustedDataError("inconsistent OHLC range")
        bar = {
            "trade_date": day.isoformat(), **prices,
            "volume_shares": _number(row["volume"], "volume_shares", integer=True),
            "amount_cny": _number(row["amount"], "amount_cny"),
            "tradestatus": _state(row.get("tradestatus"), "tradestatus") if is_stock else None,
            "is_st": _state(row.get("isST"), "isST") if is_stock else None,
            "raw_hash": canonical_hash(row),
        }
        quality = [f"missing_{key}" for key in (*prices, "volume_shares", "amount_cny") if bar[key] is None]
        if is_stock:
            quality.extend(f"missing_{key}" for key in ("tradestatus", "is_st") if bar[key] is None)
        volume, amount = bar["volume_shares"], bar["amount_cny"]
        if bar["tradestatus"] is False and (volume not in (None, 0) or (amount is not None and Decimal(amount) != 0)):
            raise AdjustedDataError("suspended status conflicts with nonzero volume/amount")
        if volume is not None and amount is not None and ((volume == 0) != (Decimal(amount) == 0)):
            quality.append("volume_amount_inconsistent")
        bar["quality_flags"] = quality
        issues.extend(f"{day.isoformat()}:{flag}" for flag in quality)
        bars.append(bar)
    missing = sorted(dates - seen)
    issues.extend(f"missing_trade_date:{day.isoformat()}" for day in missing)
    if not rows:
        issues.append("empty_confirmed")
    elif max(seen) != trading_dates[-1]:
        issues.append("stale_target_date")
    return {
        "symbol": symbol, "security_type": security_type, "provider": "baostock",
        "adjustment_mode": "forward_adjusted" if is_stock else "index_native",
        "price_unit": "CNY" if is_stock else "index_points", "amount_unit": "CNY",
        "volume_unit": "shares", "parameters": parameters,
        "fetch_version": "sha256:" + canonical_hash({"raw_hash": response["raw_hash"], "fetched_at": response["fetched_at"], "parameters": parameters}),
        "fetched_at": response["fetched_at"], "first_seen_at": response["fetched_at"],
        "raw_hash": response["raw_hash"], "sdk_version": response.get("sdk_version"),
        "actual_latest_data_date": max(seen).isoformat() if seen else None,
        "bars": sorted(bars, key=lambda item: item["trade_date"]), "issues": issues,
    }


def prepare_adjusted_data(
    symbol_types: dict[str, str], trading_dates: list[date], output_dir: Path,
    client: BaoStockClient | Any | None = None,
) -> dict[str, Any]:
    """Fetch <=10 configured samples and freeze a new, independently hashed bundle.

    The caller supplies trading dates read from the validated M1 calendar. This
    function never invents a weekday calendar, expands the sample, or reads/writes
    SQLite. Any injected non-BaoStock client is conspicuously marked offline_test.
    """
    if not isinstance(symbol_types, dict) or not 1 <= len(symbol_types) <= MAX_SAMPLE_SIZE:
        raise ValueError("M2 permits 1 to 10 configured technical samples")
    if (not isinstance(trading_dates, list) or not 1 <= len(trading_dates) <= MAX_TRADING_DATES
            or any(type(day) is not date for day in trading_dates)
            or trading_dates != sorted(set(trading_dates))):
        raise ValueError("trading_dates must be 1 to 260 unique ascending calendar dates")
    for symbol, security_type in symbol_types.items():
        validate_request("history", {"code": symbol, "security_type": security_type,
                                    "start_date": trading_dates[0].isoformat(), "end_date": trading_dates[-1].isoformat()})
    client = client if client is not None else BaoStockClient()
    live = type(client) is BaoStockClient
    output_dir = Path(output_dir).resolve()
    if not live:
        project = Path(__file__).resolve().parents[2]
        protected = [project / "data" / "research", project / "outputs" / "research"]
        if any(output_dir == path or path in output_dir.parents for path in protected):
            raise ValueError("offline_test clients cannot write into real research directories")
    created_at = datetime.now(SHANGHAI)
    batch_id = created_at.strftime("%Y%m%dT%H%M%S%f") + "-adjusted-" + uuid4().hex[:8]
    run_dir = output_dir / batch_id
    responses_dir = run_dir / "responses"
    responses_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema_version": "m2-adjusted-bundle-v1", "batch_id": batch_id,
        "mode": "research" if live else "offline_test",
        "verification_kind": "live_network" if live else "offline_test",
        "scope": "小样本验证；股票前复权趋势窗口与原生指数基准，仅技术验证。",
        "created_at": created_at.isoformat(), "run_directory": str(run_dir),
        "start_date": trading_dates[0].isoformat(), "end_date": trading_dates[-1].isoformat(),
        "trading_dates": [day.isoformat() for day in trading_dates],
        "symbol_types": dict(sorted(symbol_types.items())), "series": {}, "failures": [],
        "adjustment_consistency": "Each symbol is fetched as one full window; server atomic version is unavailable. Old bundles are never overwritten.",
        "delisting_period_status": "unknown",
        "delisting_period_reason": "BaoStock basic status/outDate and daily isST/tradestatus do not identify a delisting-consolidation period.",
    }
    stop_reason: str | None = None
    for symbol, security_type in sorted(symbol_types.items()):
        if stop_reason:
            manifest["failures"].append({"symbol": symbol, "status": "not_attempted", "reason": stop_reason})
            continue
        parameters = {"code": symbol, "security_type": security_type,
                      "start_date": manifest["start_date"], "end_date": manifest["end_date"],
                      "adjustment_mode": "forward_adjusted" if security_type == "stock" else "unadjusted"}
        response = client.query("history", **parameters)
        response_path = responses_dir / f"{symbol}.json"
        _write_new(response_path, {"verification_kind": manifest["verification_kind"], "result": response})
        if not response.get("ok"):
            status = response.get("status", "unknown")
            manifest["failures"].append({"symbol": symbol, "status": status, "error_code": response.get("error_code"),
                                         "reason": response.get("error_msg", "provider failure"), "response_path": str(response_path)})
            if status in {"permission_denied", "rate_limited", "schema_changed"}:
                stop_reason = f"source stopped after {symbol}: {status}"
            continue
        try:
            series = normalize_adjusted_response(response, symbol=symbol, security_type=security_type, trading_dates=trading_dates)
        except (AdjustedDataError, TypeError, KeyError, ValueError) as exc:
            manifest["failures"].append({"symbol": symbol, "status": "validation_failed", "reason": str(exc), "response_path": str(response_path)})
            continue
        series["response_path"] = str(response_path)
        manifest["series"][symbol] = series
        if series["issues"]:
            manifest["failures"].append({"symbol": symbol, "status": "partial", "reason": "; ".join(series["issues"]), "response_path": str(response_path)})
    manifest["success_count"] = sum(not series["issues"] for series in manifest["series"].values())
    manifest["failure_count"] = len(manifest["failures"])
    manifest["status"] = "ok" if not manifest["failures"] else ("partial" if manifest["series"] else "failed")
    manifest["manifest_hash"] = canonical_hash(manifest)
    _write_new(run_dir / "manifest.json", manifest)
    return manifest
