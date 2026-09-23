"""Explicit, isolated native-index window for the existing F3 relative rule."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
from pathlib import Path
import time
import uuid

from .m2_data import normalize_adjusted_response
from .providers.baostock import BaoStockClient, SHANGHAI, validate_request
from .providers.baostock_f2 import BaoStockF2Client, resolve_history_calendar
from .providers.sector_status import _permission
from .sector_history import _CacheOnlyCalendar, _inputs, _io, _now, _archive_path, _read, _seal, _verified_file, _write_new

SYMBOL = "sh.000001"


def _dates(root, selection, config):
    _, mode, directory, _, calendar_path = _inputs(root, selection, config)
    calendar = resolve_history_calendar(date.fromisoformat(selection["target_date"]), calendar_path,
        history_days=21, client=_CacheOnlyCalendar(), mode=mode)
    if calendar.get("verified") is not True or len(calendar.get("trading_dates", [])) != 21 or calendar["trading_dates"][-1] != selection["target_date"]:
        raise ValueError("benchmark requires 21 exact trusted dates ending at target")
    return directory, calendar


def _series(response, expected, calendar, *, mode):
    operation = response.get("operation")
    if operation not in {"history", "history_f2"}:
        raise ValueError("benchmark SDK operation invalid")
    params = validate_request(operation, response.get("parameters", {}))
    if params.get("code") != SYMBOL or params.get("security_type") != "index" or params.get("adjustment_mode") != "unadjusted":
        raise ValueError("benchmark is not the configured native index")
    if not BaoStockClient._valid_worker_result(response, operation, params) or not response.get("ok"):
        raise ValueError("benchmark SDK response not verified")
    observed = datetime.fromisoformat(response["fetched_at"])
    if observed > datetime.now(SHANGHAI) or observed.date().isoformat() < expected[-1]:
        raise ValueError("benchmark observation time invalid")
    if response.get("provenance_mode", "online" if mode == "research" else "offline_test") != ("online" if mode == "research" else "offline_test"):
        raise ValueError("benchmark source provenance invalid")
    if not params["start_date"] <= expected[0] <= expected[-1] <= params["end_date"]:
        raise ValueError("benchmark source window excludes requested dates")
    source_dates = [day for day, opened in sorted(calendar["calendar"].items()) if opened and params["start_date"] <= day <= params["end_date"]]
    # Never change source parameters to pretend an archived broad window was a
    # narrower physical response. Validate the original full window first.
    series = normalize_adjusted_response(response, symbol=SYMBOL, security_type="index",
        trading_dates=[date.fromisoformat(day) for day in source_dates])
    rows = [row for row in series["bars"] if row["trade_date"] in expected]
    if [row["trade_date"] for row in rows] != expected or any(row["quality_flags"] for row in rows):
        raise ValueError("benchmark exact aligned index window incomplete")
    return {**series, "records": rows, "bars": rows, "expected_dates": expected,
        "source_window_start": params["start_date"], "source_window_end": params["end_date"],
        "physical_source_rows": len(response["rows"]), "issues": [], "status": "ready",
        "security_type": "index", "adjustment_mode": "index_native", "price_unit": "index_points"}


def _read_archive(path, *, mode):
    body = _io(path).read_bytes()
    packet = _read(path)
    response = packet.get("result", packet)
    if "result" in packet and packet.get("verification_kind") != ("live_network" if mode == "research" else "offline_test"):
        raise ValueError("benchmark archive provenance mismatch")
    return response, hashlib.sha256(body).hexdigest()


def read_benchmark(root, selection, config):
    root = Path(root).resolve()
    directory, calendar = _dates(root, selection, config)
    path = directory / "benchmark/benchmark_snapshot.json"
    if not path.is_file():
        return {"status": "missing", "verified": False, "symbol": SYMBOL, "security_type": "index", "adjustment_mode": "index_native",
            "price_unit": "index_points", "records": [], "expected_dates": calendar["trading_dates"],
            "issues": ["benchmark_not_prepared"], "file_refs": [], "network_requests": 0}
    packet = _verified_file(path)
    if packet["selection_id"] != selection["selection_id"] or packet["selection_hash"] != selection["content_hash"]:
        raise ValueError("benchmark frozen selection mismatch")
    reference = packet["source_reference"]
    source = _archive_path(root, reference["path"], selection["mode"])
    response, signature = _read_archive(source, mode=selection["mode"])
    if signature != reference["sha256"]:
        raise ValueError("benchmark original source hash mismatch")
    series = _series(response, calendar["trading_dates"], calendar, mode=selection["mode"])
    if series != packet["series"]:
        raise ValueError("benchmark frozen normalized values differ")
    return {**series, "verified": True, "selection_id": selection["selection_id"], "path": str(path), "model_calls": 0,
        "purpose": packet["purpose"], "production_eligible": packet["production_eligible"],
        "file_refs": [reference, {"path": str(path), "sha256": hashlib.sha256(_io(path).read_bytes()).hexdigest()}],
        "network_requests": 0, "source_business_date": None, "cache_replay": True,
        "historical_reconstruction": packet["historical_reconstruction"], "metrics": {"network_requests": 0, "requests": 0, "retries": 0, "rows": len(series["records"])}}


def prepare_benchmark(root, selection, config, *, online=False, max_seconds=20):
    root = Path(root).resolve()
    directory, calendar = _dates(root, selection, config)
    if not selection["members"]:
        return {"status": "not_applicable", "symbol": SYMBOL, "records": [], "issues": [], "network_requests": 0}
    _permission(root)
    frozen = directory / "benchmark/benchmark_snapshot.json"
    if frozen.is_file():
        return read_benchmark(root, selection, config)
    expected, began = calendar["trading_dates"], time.monotonic()
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (float, int)) or not 0 < max_seconds <= 120:
        raise ValueError("benchmark timeout must be finite")
    run = directory / "benchmark/runs" / uuid.uuid4().hex
    plan = {"selection_id": selection["selection_id"], "selection_hash": selection["content_hash"],
        "purpose": selection.get("purpose", "production"), "production_eligible": selection.get("production_eligible", True),
        "symbol": SYMBOL, "name": "上证综合指数", "security_type": "index", "adjustment_mode": "index_native",
        "expected_dates": expected, "calendar": calendar, "max_network_requests": 1, "max_attempts": 1,
        "parameters": dict(code=SYMBOL, start_date=expected[0], end_date=expected[-1], security_type="index", adjustment_mode="unadjusted"),
        "request_reason": "necessary_existing_relative_strength_benchmark_exact_21_dates", "created_at": _now()}
    _write_new(run / "plan.json", _seal(plan))
    source, response, series, signature, errors = None, None, None, None, []
    if selection["mode"] == "research":
        for base in (root / "data/research/m21_adjusted", root / "data/research/m2_adjusted"):
            for candidate in sorted(base.glob("*/responses/sh.000001.json"), reverse=True):
                try:
                    possible, hashed = _read_archive(candidate, mode="research")
                    validated = _series(possible, expected, calendar, mode="research")
                    source, response, series, signature = candidate, possible, validated, hashed
                    break
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    errors.append({"path": str(candidate), "reason": str(exc)})
            if series is not None:
                break
    requests = 0
    if series is None and online:
        if selection["mode"] != "research":
            raise ValueError("online benchmark rejects offline_test")
        # The SDK's single-request hard timeout includes login. No reconnect or
        # retry follows a login, network, schema, permission or source failure.
        with BaoStockF2Client(timeout_seconds=min(30, max_seconds), max_attempts=1, pause_seconds=0) as client:
            response = client.query("history_f2", **plan["parameters"])
        requests = 1
        source = run / "source-response.json"
        signature = _write_new(source, response)
        try:
            series = _series(response, expected, calendar, mode="research")
        except (ValueError, KeyError, TypeError) as exc:
            errors.append({"path": str(source), "reason": str(exc), "status": response.get("status"),
                "error_code": response.get("error_code"), "diagnostics": response.get("diagnostics")})
    result = {"status": "ready" if series else "benchmark_pending", "symbol": SYMBOL, "security_type": "index",
        "verified": series is not None, "selection_id": selection["selection_id"], "model_calls": 0,
        "adjustment_mode": "index_native", "price_unit": "index_points", "network_requests": requests, "retries": 0,
        "purpose": plan["purpose"], "production_eligible": plan["production_eligible"], "errors": errors,
        "records": [], "issues": [] if series else ["benchmark_target_window_missing"],
        "run_directory": str(run), "elapsed_seconds": round(time.monotonic()-began, 6), "source_business_date": None}
    if series:
        reference = {"path": str(source), "sha256": signature}
        packet = _seal({"schema_version": "f3s-benchmark-v1", "selection_id": selection["selection_id"],
            "selection_hash": selection["content_hash"], "purpose": plan["purpose"], "production_eligible": plan["production_eligible"],
            "series": series, "source_reference": reference, "created_at": _now(),
            "historical_reconstruction": datetime.fromisoformat(series["fetched_at"]).date().isoformat() > selection["target_date"]})
        _write_new(frozen, packet)
        result.update(read_benchmark(root, selection, config))
        result.update(network_requests=requests, cache_replay=requests == 0)
    result["metrics"] = {"network_requests": requests, "requests": requests, "retries": 0,
        "rows": len(series["records"]) if series else 0, "elapsed_seconds": round(time.monotonic()-began, 6)}
    result.setdefault("path", str(run / "result.json"))
    _write_new(run / "result.json", _seal(result))
    return result
