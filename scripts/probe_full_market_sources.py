"""F0 bounded, observable BaoStock probes; no credentials, model, or new source.

This is a diagnostic command, not the F2 production daily-bar pipeline.
Every response is archived separately. Optional documentation reads only
retrieve previously identified official pages and never enable their API.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from urllib.request import Request, urlopen

from ashare_daily.providers.baostock import (
    BASIC_FIELDS, HISTORY_STOCK_FIELDS, SHANGHAI, BaoStockClient,
    base_result, clean_log, classify_error, raw_hash,
)
from ashare_daily.providers.baostock_worker import check_result, read_rows, ProviderError


SAMPLES = {
    "sse_main": "sh.600000", "szse_main": "sz.000001",
    "chinext": "sz.300750", "star": "sh.688981", "bse": "bj.920163",
}
SAMPLE_BASIS = {
    "sse_main": "docs/09_BAOSTOCK_VERIFICATION.md#小样本身份和使用范围",
    "szse_main": "docs/09_BAOSTOCK_VERIFICATION.md#小样本身份和使用范围",
    "chinext": "https://www.szse.cn/disclosure/notice/company/t20180607_539221.html",
    "star": "https://www.sse.com.cn/disclosure/announcement/listing/c/c_20200713_5152912.shtml",
    "bse": "https://www.bse.cn/disclosure/2025/2025-10-29/1ba1b5548b3943ff9e2c0970ce0ddfda.pdf",
}
DOCS = [
    ("baostock_home", "https://www.baostock.com/helpdocs/api/markdown/home.md", "POST"),
    ("baostock_basic", "https://www.baostock.com/helpdocs/api/markdown/stockBasic.md", "POST"),
    ("baostock_metadata", "https://www.baostock.com/helpdocs/api/markdown/StockBasicInfoAPI.md", "POST"),
    ("baostock_history", "https://www.baostock.com/helpdocs/api/markdown/stockKData.md", "POST"),
    ("baostock_adjustment", "https://www.baostock.com/helpdocs/api/markdown/dataExplain.md", "POST"),
    ("akshare_stock", "https://akshare.akfamily.xyz/data/stock/stock.html", "GET"),
    ("akshare_special", "https://akshare.akfamily.xyz/special.html", "GET"),
    *[(f"tushare_{doc_id}", f"https://tushare.pro/document/2?doc_id={doc_id}", "GET") for doc_id in (25, 26, 27, 28, 375)],
]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sdk_probe(operation, code, start, end):
    """Only used inside a hard-timeout subprocess for capability boundaries."""
    import baostock as sdk
    result = base_result(operation, {"code": code, "start_date": start, "end_date": end})
    log = io.StringIO()
    started, logged_in = time.monotonic(), False
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        try:
            login = sdk.login()
            result["login"] = {"ok": login.error_code == "0", "error_code": login.error_code, "error_msg": clean_log(login.error_msg)}
            check_result(login)
            logged_in = True
            if operation == "sdk_basic_probe":
                response, fields = sdk.query_stock_basic(code=code), BASIC_FIELDS
            elif operation == "sdk_history_probe":
                fields = HISTORY_STOCK_FIELDS
                response = sdk.query_history_k_data_plus(code, ",".join(fields), start_date=start, end_date=end, frequency="d", adjustflag="3")
            else:
                fields = ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"]
                response = sdk.query_adjust_factor(code=code, start_date=start, end_date=end)
            actual_fields, rows = read_rows(response, fields)
            result.update(ok=True, status="ok" if rows else "empty_confirmed", error_code="0", error_msg=response.error_msg,
                          fields=actual_fields, rows=rows, raw_hash=raw_hash(actual_fields, rows))
        except ProviderError as exc:
            result.update(error_code=exc.code, error_msg=exc.message, status=classify_error(exc.code, exc.message))
        except Exception as exc:
            result.update(error_code="probe_exception", error_msg=clean_log(f"{type(exc).__name__}: {exc}"))
        finally:
            if logged_in:
                sdk.logout()
    result.update(sdk_log=clean_log(log.getvalue()), elapsed_seconds=round(time.monotonic() - started, 6))
    return result


def isolated_probe(operation, code, start, end):
    command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--sdk-worker", operation,
               "--code", code, "--start", start, "--date", end]
    options = {"text": True, "encoding": "utf-8", "capture_output": True, "timeout": 20, "check": False}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(command, **options)
        result = json.loads(completed.stdout)
        if completed.returncode or not isinstance(result, dict):
            raise ValueError("invalid probe protocol")
        return result
    except (subprocess.TimeoutExpired, ValueError) as exc:
        result = base_result(operation, {"code": code, "start_date": start, "end_date": end})
        result.update(status="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "schema_changed",
                      error_code="worker_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "probe_protocol_error",
                      error_msg=clean_log(str(exc)))
        return result


def probe_documents(output):
    records = []
    for name, url, method in DOCS:
        started = time.monotonic()
        record = {"document": name, "url": url, "method": method, "checked_at": datetime.now(SHANGHAI).isoformat()}
        try:
            request = Request(url, data=b"" if method == "POST" else None, method=method, headers={"User-Agent": "ashare-daily-research/F0-document-verification"})
            with urlopen(request, timeout=15) as response:
                body = response.read(3_000_001)
                if len(body) > 3_000_000:
                    raise ValueError("documentation exceeds 3MB bound")
                record.update(http_status=response.status, final_url=response.url, bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
                text = body.decode("utf-8", errors="replace")
                # Only a document response is retained, never arbitrary sites,
                # cookies, request headers, tokens or credentials.
                (output / f"document-{name}.txt").write_text(text, encoding="utf-8")
                record["path"] = f"document-{name}.txt"
        except Exception as exc:
            record.update(error=clean_log(f"{type(exc).__name__}: {exc}"))
        record["elapsed_seconds"] = round(time.monotonic() - started, 6)
        records.append(record)
    save(output / "documents.json", records)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--calendar-only", action="store_true")
    parser.add_argument("--lists-only", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=20, choices=range(1, 121))
    parser.add_argument("--docs", action="store_true")
    parser.add_argument("--environment-label", default="current-workstation")
    parser.add_argument("--sdk-worker", choices=["sdk_basic_probe", "sdk_history_probe", "sdk_adjust_probe"], help=argparse.SUPPRESS)
    parser.add_argument("--code", choices=[*SAMPLES.values(), "bj.838163"], help=argparse.SUPPRESS)
    parser.add_argument("--start", type=date.fromisoformat, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.sdk_worker:
        if not args.start or not args.code or not 0 <= (args.date - args.start).days <= 365:
            parser.error("bounded worker requires explicit sample and dates")
        print(json.dumps(sdk_probe(args.sdk_worker, args.code, args.start.isoformat(), args.date.isoformat()), ensure_ascii=False))
        return 0
    if args.output is None:
        parser.error("--output is required")
    args.output.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(SHANGHAI).isoformat()
    client = BaoStockClient(timeout_seconds=args.timeout_seconds, max_attempts=1)
    records = []
    source_stopped = None

    def record(name, response, board="all", dataset=None):
        nonlocal source_stopped
        save(args.output / f"{name}.json", response)
        item = {"id": name, "dataset": dataset or response["operation"], "board": board, "response_path": f"{name}.json",
                "support_status": "verified" if response["ok"] and response["rows"] else "unverified" if response["ok"] else "failed",
                "ok": response["ok"], "error_code": response["error_code"], "rows": len(response["rows"]),
                "fields": response["fields"], "elapsed_seconds": response["elapsed_seconds"], "fetched_at": response["fetched_at"],
                "login": response["login"], "parameters": response["parameters"], "pagination": response.get("pagination"),
                "latest_data_date": max((row["date"] for row in response["rows"] if "date" in row), default=None),
                "requested_date_present": any(row.get("date") == args.date.isoformat() for row in response["rows"]) if (dataset or response["operation"]) == "history" else None}
        records.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
        if response["status"] in {"permission_denied", "rate_limited", "schema_changed"}:
            source_stopped = f"{name}: {response['status']} / {response['error_code']}"

    def probe(name, factory, board="all", dataset=None):
        if source_stopped is None:
            record(name, factory(), board, dataset)

    dates = {"start_date": (args.date - timedelta(days=40)).isoformat(), "end_date": args.date.isoformat()}
    calendar = client.query("calendar", **dates)
    record("calendar", calendar)
    if not args.calendar_only and calendar["login"]["ok"]:
        probe("basic-all", lambda: client.query("basic_all"))
        probe("universe", lambda: client.query("universe", day=args.date.isoformat()))
        start = (args.date - timedelta(days=4)).isoformat()
        for board, code in ([] if args.lists_only else SAMPLES.items()):
            time.sleep(0.5)
            if board == "bse":
                # This explicitly probes SDK capabilities; the production
                # history/basic adapter retains its reviewed sh/sz contract.
                probe(f"{board}-basic", lambda: isolated_probe("sdk_basic_probe", code, start, args.date.isoformat()), board, "basic")
                probe(f"{board}-history", lambda: isolated_probe("sdk_history_probe", code, start, args.date.isoformat()), board, "history")
                probe("bse-old-basic", lambda: isolated_probe("sdk_basic_probe", "bj.838163", start, args.date.isoformat()), board, "old_code_basic")
            else:
                probe(f"{board}-basic", lambda: client.query("basic", code=code), board)
                for mode in ("unadjusted", "forward_adjusted"):
                    probe(f"{board}-{mode}", lambda: client.query("history", code=code, start_date=start, end_date=args.date.isoformat(), security_type="stock", adjustment_mode=mode), board)
            probe(f"{board}-adjust-factor", lambda: isolated_probe("sdk_adjust_probe", code, (args.date - timedelta(days=365)).isoformat(), args.date.isoformat()), board, "adjust_factor")
    documents = probe_documents(args.output) if args.docs else []
    package_versions = {}
    for name in ("baostock", "akshare", "tushare"):
        try:
            package_versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            package_versions[name] = "not_installed"
    manifest = {"schema_version": "f0-source-probe-v1", "mode": "read_only_diagnostic", "requested_date": args.date.isoformat(),
                "started_at": started_at, "completed_at": datetime.now(SHANGHAI).isoformat(), "environment": args.environment_label,
                "sdk_default_endpoint": "public-api.baostock.com:10030", "installed_versions": package_versions,
                "request_count": len(records), "records": records, "documents": documents, "sample_identity_basis": SAMPLE_BASIS,
                "all_market_coverage_verified": False, "model_calls": 0,
                "source_stop_reason": source_stopped if calendar["login"]["ok"] else "BaoStock login failed; dependent probes not sent",
                "disabled_sources": [{"provider": "akshare", "status": "permission_required", "reason": "academic/noncommercial and upstream usage permission not confirmed; not enabled"},
                                     {"provider": "tushare", "status": "permission_required", "reason": "account/interface points and data usage permission not confirmed; no token read or API call"}]}
    save(args.output / "manifest.json", manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
