"""Four-board, 320-trading-date BaoStock F2 entry probe; no full-market run."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from ashare_daily.providers.baostock import SHANGHAI
from ashare_daily.providers.baostock_f2 import BaoStockF2Client, resolve_history_calendar


SAMPLES = {"sse_main": "sh.600000", "szse_main": "sz.000001", "chinext": "sz.300750", "star": "sh.688981"}
DOCS = {
    "history": ("https://www.baostock.com/mainContent?file=stockKData.md", "outputs/verification/f0-f1/probe-live-20260911/document-baostock_history.txt"),
    "adjustment": ("https://www.baostock.com/mainContent?file=dataExplain.md", "outputs/verification/f0-f1/probe-live-20260911/document-baostock_adjustment.txt"),
}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def check_window(response: dict, code: str, dates: list[str], mode: str) -> dict:
    issues = []
    quality_flags = []
    expected = set(dates)
    rows = response["rows"] if response["ok"] else []
    seen = set()
    for row in rows:
        key = row.get("date")
        if key in seen:
            issues.append({"date": key, "kind": "duplicate_date"})
        seen.add(key)
        if key not in expected or row.get("code") != code:
            issues.append({"date": key, "kind": "date_or_identity_mismatch"})
        if row.get("adjustflag") != ("3" if mode == "unadjusted" else "2"):
            issues.append({"date": key, "kind": "adjustment_flag_mismatch"})
        if row.get("tradestatus") not in {"0", "1"} or row.get("isST") not in {"0", "1"}:
            issues.append({"date": key, "kind": "unknown_status"})
        for field, flag in (("volume", "missing_volume_shares"), ("amount", "missing_amount_cny")):
            if row.get(field) in {None, ""}:
                quality_flags.append({"date": key, "flag": flag, "tradestatus": row.get("tradestatus")})
        try:
            values = {k: Decimal(row[k]) for k in ("open", "high", "low", "close", "preclose", "volume", "amount")}
            if any(not value.is_finite() for value in values.values()):
                raise ValueError("nonfinite")
            if any(values[k] <= 0 for k in ("open", "high", "low", "close", "preclose")):
                raise ValueError("nonpositive_price")
            if not (values["low"] <= values["open"] <= values["high"] and values["low"] <= values["close"] <= values["high"]):
                raise ValueError("ohlc_range")
            if values["volume"] < 0 or values["volume"] != values["volume"].to_integral_value() or values["amount"] < 0:
                raise ValueError("volume_or_amount")
        except (InvalidOperation, ValueError, KeyError) as exc:
            issues.append({"date": key, "kind": "numeric_contract", "detail": str(exc)})
    missing = sorted(expected - seen)
    return {"verified": response["ok"] and not issues and not missing and len(rows) == len(dates),
            "window_response_complete": (response["ok"] and not missing and len(rows) == len(dates)
                                         and not any(i["kind"] in {"duplicate_date", "date_or_identity_mismatch", "adjustment_flag_mismatch"} for i in issues)),
            "response_ok": response["ok"], "rows": len(rows), "expected_trading_dates": len(dates),
            "first_date": min(seen) if seen else None, "latest_date": max(seen) if seen else None,
            "target_date_present": bool(dates and dates[-1] in seen), "missing_dates": missing, "issues": issues,
            "quality_flags": quality_flags,
            "status_counts": {"normal_trade": sum(r.get("tradestatus") == "1" for r in rows),
                              "suspended": sum(r.get("tradestatus") == "0" for r in rows),
                              "st_true": sum(r.get("isST") == "1" for r in rows)},
            "units": {"price": "CNY", "volume": "shares", "amount": "CNY"},
            "adjustment": {"mode": mode, "full_window": True,
                           "anchor_basis": "provider_current_at_fetch" if mode == "forward_adjusted" else "unadjusted",
                           "historical_as_of_supported": False, "factor_independently_verified": False,
                           "fetched_at": response["fetched_at"], "raw_hash": response["raw_hash"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--calendar-cache", type=Path, default=Path("data/research/f2_calendar"))
    parser.add_argument("--environment-label", required=True, choices=["workstation-network-enabled"])
    args = parser.parse_args()
    permission = json.loads(Path("config/universe.json").read_text(encoding="utf-8"))
    if not any(s["provider"] == "baostock" and s["enabled"] is True and s["permission_status"] == "approved" for s in permission["sources"]):
        parser.error("BaoStock current local permission is not enabled/approved")
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": "f2-baostock-entry-probe-v1", "verification_kind": "live_network",
                "environment": args.environment_label, "scope": "sse_szse_a", "excluded_scope": ["bse"],
                "requested_date": args.date.isoformat(), "started_at": datetime.now(SHANGHAI).isoformat(),
                "history_days": 320, "sample_identity_basis": "F1.1 current source-backed identities; explicit four-board source probe samples",
                "records": [], "calendar_verified": False, "sample_entry_verified": False,
                "full_market_verified": False, "model_calls": 0, "source_stop_reason": None,
                "limits": {"concurrency": 1, "attempts_per_request": 1, "timeout_seconds_per_request": 20,
                           "max_calendar_requests": 2, "max_history_requests": 8}, "official_document_snapshots": []}
    for name, (url, path) in DOCS.items():
        body = Path(path).read_bytes()
        manifest["official_document_snapshots"].append({"name": name, "url": url, "path": path,
                                                        "sha256": hashlib.sha256(body).hexdigest(), "retrieved_on": "2026-09-11"})
    with BaoStockF2Client(timeout_seconds=20, max_attempts=1) as client:
        calendar = resolve_history_calendar(args.date, args.calendar_cache, history_days=320, client=client)
        save(args.output / "calendar.json", calendar)
        manifest["calendar_verified"] = calendar["verified"]
        manifest["calendar_path"] = "calendar.json"
        manifest["calendar_sha256"] = hashlib.sha256((args.output / "calendar.json").read_bytes()).hexdigest()
        print(json.dumps({"event": "calendar", "verified": calendar["verified"], "trading_dates": len(calendar["trading_dates"]),
                          "window_start": calendar.get("window_start"), "window_end": calendar.get("window_end")}, ensure_ascii=False), flush=True)
        if calendar["verified"]:
            for board, code in SAMPLES.items():
                for mode in ("unadjusted", "forward_adjusted"):
                    response = client.query("history_f2", code=code, start_date=calendar["window_start"],
                                            end_date=calendar["window_end"], security_type="stock", adjustment_mode=mode)
                    path = args.output / f"{board}-{mode}.json"
                    save(path, response)
                    checked = check_window(response, code, calendar["trading_dates"], mode)
                    record = {"board": board, "code": code, "mode": mode, "response_path": path.name,
                              "response_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "parameters": response["parameters"],
                              "elapsed_seconds": response["elapsed_seconds"], "login": response["login"],
                              "session": response.get("session"), "status": response["status"], "error_code": response["error_code"],
                              "verification": checked}
                    manifest["records"].append(record)
                    save(args.output / "manifest.json", manifest)
                    print(json.dumps({"board": board, "mode": mode, "verified": checked["verified"], "rows": checked["rows"],
                                      "latest_date": checked["latest_date"], "elapsed_seconds": response["elapsed_seconds"],
                                      "status": response["status"], "error_code": response["error_code"], "session": response.get("session")}, ensure_ascii=False), flush=True)
                    if response["status"] in {"permission_denied", "rate_limited", "schema_changed"}:
                        manifest["source_stop_reason"] = f"{board}/{mode}: {response['status']}"
                        break
                if manifest["source_stop_reason"]:
                    break
        else:
            manifest["source_stop_reason"] = f"calendar: {calendar.get('validation_error', calendar['status'])}"
    manifest["sample_entry_verified"] = len(manifest["records"]) == 8 and all(r["verification"]["verified"] for r in manifest["records"])
    manifest["completed_at"] = datetime.now(SHANGHAI).isoformat()
    save(args.output / "manifest.json", manifest)
    return 0 if manifest["sample_entry_verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
