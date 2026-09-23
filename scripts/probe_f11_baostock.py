"""F1.1 bounded anonymous BaoStock diagnostic, separate from production sync.

Run only in the explicitly network-enabled workstation context. No token,
model, market DB writes, full-price initialization or synthetic fallback.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys

from ashare_daily.providers.baostock import BaoStockClient, SHANGHAI, clean_log
from ashare_daily.quality.baostock import normalize_calendar
from probe_full_market_sources import isolated_probe, SAMPLE_BASIS


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def failure_category(response: dict) -> str | None:
    """Do not turn network/data/SDK errors into purchase or permission advice."""
    if response["status"] == "permission_denied":
        return "source_access_denied"
    if response["status"] == "rate_limited":
        return "source_rate_limited"
    if response["ok"]:
        return "source_empty_result" if not response["rows"] else None
    if not response["login"]["ok"]:
        return "login_failed"
    # Interpret old v1 evidence as well: it emitted row_read before checking
    # an already-failed query response. Never overwrite the original record.
    query_errors = [event for event in response.get("diagnostics", {}).get("events", [])
                    if event.get("stage") == "query_wait" and event.get("state") == "completed"
                    and isinstance(event.get("error_code"), str) and event["error_code"] != "0"]
    if query_errors:
        return "query_response_failed"
    stage = response.get("diagnostics", {}).get("failure_stage")
    if stage in {"query_wait", "page_wait", "terminal_validation", "row_read", "logout_wait"}:
        return f"{stage}_failed"
    if response["error_code"].startswith("100040"):
        return "source_rejected_capability_parameters"
    return "source_query_failed"


def run_probe(target: date, output: Path, environment: str, *, client=None, sdk_sample=None) -> dict:
    """Injectable only for offline tests; test evidence cannot use live labels."""
    injected = client is not None or sdk_sample is not None
    if injected and environment != "offline_test":
        raise ValueError("injected clients must use offline_test, never live evidence")
    if not injected and environment != "workstation-network-enabled":
        raise ValueError("real probes require the explicitly approved workstation-network-enabled environment")
    client = client or BaoStockClient(timeout_seconds=20, max_attempts=1, diagnostic_stages=True)
    sdk_sample = sdk_sample or isolated_probe
    output.mkdir(parents=True, exist_ok=False)
    records: list[dict] = []
    manifest = {
        "schema_version": "f11-baostock-probe-v1", "verification_kind": "offline_test" if injected else "live_network",
        "environment": environment, "started_at": datetime.now(SHANGHAI).isoformat(),
        "requested_date": target.isoformat(), "target_is_trading_day": None,
        "sample_latest_data_date": None, "sample_target_date_present": None,
        "discovery_date": None, "discovery_is_target_date": None,
        "source": "baostock", "permission_basis": "config/universe.json#sources/baostock",
        "transport": "anonymous official SDK public-api.baostock.com:10030",
        "limits": {"attempts_per_request": 1, "hard_timeout_seconds": 20, "discovery_requests_max": 2,
                   "total_requests_max": 7, "concurrency": 1},
        "request_count": 0, "records": records, "source_stop_reason": None,
        "all_a_route": "closed_insufficient_coverage_and_metadata", "universe_verified": False,
        "board_market_totals": dict.fromkeys(["sse_main", "szse_main", "chinext", "star", "bse"]),
        "model_calls": 0, "full_market_price_initialization": False,
        "coverage_limitations": [
            "BaoStock documented all-stock scope is Shanghai/Shenzhen, no verified BSE coverage",
            "basic type=1 is generic stock, not independent evidence of ordinary-A versus B/CDR",
            "basic/all-stock fields do not provide authoritative five-board metadata",
            "basic_all is current metadata without historical as-of parameter",
            "short quote samples cannot prove all-market quote or research completeness",
        ],
    }

    def record(name: str, response: dict) -> None:
        path = output / f"{name}.json"
        write_json(path, response)
        item = {"id": name, "response_path": path.name,
                "response_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "parameters": response["parameters"], "login": response["login"],
                "ok": response["ok"], "error_code": response["error_code"],
                "status": response["status"], "failure_category": failure_category(response),
                "rows": len(response["rows"]), "fields": response["fields"],
                "elapsed_seconds": response["elapsed_seconds"], "fetched_at": response["fetched_at"],
                "diagnostics": response.get("diagnostics"), "pagination": response.get("pagination")}
        records.append(item)
        manifest["request_count"] = len(records)
        manifest["completed_at"] = datetime.now(SHANGHAI).isoformat()
        write_json(output / "manifest.json", manifest)
        print(json.dumps({key: value for key, value in item.items() if key != "diagnostics"}, ensure_ascii=False), flush=True)
        if response["status"] in {"permission_denied", "rate_limited"}:
            manifest["source_stop_reason"] = f"{name}: {response['status']}"

    start = target - timedelta(days=7)
    calendar = client.query("calendar", start_date=start.isoformat(), end_date=target.isoformat())
    record("calendar", calendar)
    calendar_days = []
    if calendar["ok"]:
        try:
            calendar_days = normalize_calendar(calendar["rows"], start_date=start, end_date=target,
                fetched_at=datetime.fromisoformat(calendar["fetched_at"]), sdk_version=calendar["sdk_version"])
            manifest["target_is_trading_day"] = next(day.is_trading_day for day in calendar_days if day.calendar_date == target)
        except (ValueError, StopIteration) as exc:
            manifest["source_stop_reason"] = f"calendar_unverified: {clean_log(exc)}"
    else:
        manifest["source_stop_reason"] = f"calendar_unverified: {failure_category(calendar)}"
    if manifest["target_is_trading_day"] is not True:
        manifest["source_stop_reason"] = manifest["source_stop_reason"] or "trusted calendar says target is non-trading"
    else:
        sample = client.query("history", code="sh.600000", start_date=(target - timedelta(days=4)).isoformat(),
                              end_date=target.isoformat(), security_type="stock", adjustment_mode="unadjusted")
        record("update-sample-sh600000", sample)
        verified_dates = {day.calendar_date.isoformat() for day in calendar_days if day.is_trading_day}
        rows = sample["rows"] if sample["ok"] else []
        sample_dates = {row.get("date") for row in rows}
        if rows and sample_dates <= verified_dates and all(row.get("code") == "sh.600000" for row in rows):
            latest = max(sample_dates)
            manifest.update(sample_latest_data_date=latest, sample_target_date_present=target.isoformat() in sample_dates,
                            discovery_date=latest, discovery_is_target_date=latest == target.isoformat())
        else:
            manifest["source_stop_reason"] = manifest["source_stop_reason"] or "sample date/identity could not be verified; skip discovery"
        if manifest["source_stop_reason"] is None:
            # Exactly one request per endpoint; no longer-timeout second pass.
            for name, operation, parameters in [
                ("universe", "universe", {"day": manifest["discovery_date"]}),
                ("basic-all", "basic_all", {}),
            ]:
                response = client.query(operation, **parameters)
                record(name, response)
                if manifest["source_stop_reason"]:
                    break
        if manifest["source_stop_reason"] is None:
            for name, operation in [("bse-current-basic", "sdk_basic_probe"),
                                    ("bse-current-history", "sdk_history_probe"),
                                    ("bse-current-adjust-factor", "sdk_adjust_probe")]:
                response = sdk_sample(operation, "bj.920163", (target - timedelta(days=4)).isoformat(), target.isoformat())
                record(name, response)
                if manifest["source_stop_reason"]:
                    break
    manifest["bse_sample_identity_basis"] = SAMPLE_BASIS["bse"]
    manifest["completed_at"] = datetime.now(SHANGHAI).isoformat()
    write_json(output / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment-label", choices=["workstation-network-enabled"], required=True)
    args = parser.parse_args()
    run_probe(args.date, args.output, args.environment_label)
    # Completion is a diagnostic result, never F1.1 acceptance.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
