"""Bounded, anonymous BaoStock quarterly facts for an existing research shortlist.

Financial facts supplement, never rerank or approve, technical observations.
Ratios retain the provider's original strings because unit/period semantics have
not been independently reconciled. No financial data is exported to a model.
"""
from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
FIELDS = {
    "profit": ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare"],
    "cash_flow": ["code", "pubDate", "statDate", "CAToAsset", "NCAToAsset", "tangibleAssetToAsset", "ebitToInterest", "CFOToOR", "CFOToNP", "CFOToGr"],
}
LIMITATIONS = [
    "仅补充候选股的季度盈利与现金流比率，不代表公司公告、主营业务或财务报表已完成核查。",
    "保留来源原始数值；比率单位及单季/累计口径尚未复核，不转换百分比，不用于排名。",
    "现金流接口提供比率，不提供完整现金流量表；负比率不能单独证明经营现金流为负。",
    "公布日期仅精确到日；历史补跑是当前观察到的历史数据重建，不代表当时已取得该版本。",
]
NEGATIVE_PROFIT = "来源净利润为负，需核查亏损原因及持续性。"
NEGATIVE_EPS = "来源每股收益为负，需结合报表口径核查。"
NEGATIVE_CASH_RATIO = "经营现金流/净利润比率为负；仅表示比率符号，不能据此认定经营现金流为负。"


def _now():
    return datetime.now(SHANGHAI)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _seal(value):
    return {**value, "content_hash": _digest(value)}


def _time(value):
    if not isinstance(value, str):
        raise ValueError("financial timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("financial timestamp requires timezone")
    return parsed.astimezone(SHANGHAI)


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("financial date requires YYYY-MM-DD")
    return date.fromisoformat(value)


def _number(value):
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("financial fields must be bounded raw strings")
    if value == "":
        return None
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
        raise ValueError("financial value is not numeric")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("financial value is not numeric") from None
    if not result.is_finite():
        raise ValueError("financial value must be finite")
    return result


def _candidates(candidates):
    if not isinstance(candidates, list):
        raise ValueError("financial candidates must be a list")
    result = []
    for row in candidates:
        if not isinstance(row, dict):
            raise ValueError("financial candidate must be an object")
        identity, symbol, name = row.get("security_id"), row.get("symbol"), row.get("name", "")
        if not isinstance(identity, str) or not 1 <= len(identity) <= 200 or not isinstance(symbol, str) or not re.fullmatch(r"(?:sh|sz)\.\d{6}", symbol):
            raise ValueError("financial candidate requires bound identity and exchange symbol")
        if not isinstance(name, str) or len(name) > 200:
            raise ValueError("financial candidate name must be a bounded string")
        result.append({"security_id": identity, "symbol": symbol, "name": name})
    if len({r["security_id"] for r in result}) != len(result) or len({r["symbol"] for r in result}) != len(result):
        raise ValueError("financial candidates must be unique")
    return result


def _quarters(target):
    ends = [date(year, month, day) for year in (target.year - 1, target.year)
            for month, day in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return [(d.year, (d.month + 2) // 3) for d in sorted(ends, reverse=True) if d <= target][:2]


def _quarter_end(year, quarter):
    return date(year, quarter * 3, (31, 30, 30, 31)[quarter - 1])


def _validate_raw(operation, symbol, year, quarter, fields, rows):
    if fields != FIELDS[operation] or not isinstance(rows, list) or len(rows) > 1:
        raise ValueError("financial response schema mismatch")
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(fields) or row.get("code") != symbol:
            raise ValueError("financial response identity or fields mismatch")
        stat, published = _date(row["statDate"]), _date(row["pubDate"])
        if stat != _quarter_end(year, quarter) or published < stat:
            raise ValueError("financial response period mismatch")
        for field in fields[3:]:
            _number(row[field])


def _run_query(operation, symbol, year, quarter, timeout_seconds):
    """A killed worker cannot hold the daily workflow indefinitely."""
    request = {"operation": operation, "symbol": symbol, "year": year, "quarter": quarter}
    kwargs = dict(input=json.dumps(request), text=True, encoding="utf-8", errors="replace", capture_output=True,
                  timeout=timeout_seconds, check=False)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "ashare_daily.financial_review", "--worker"], **kwargs)
        if completed.returncode != 0 or len(completed.stdout) > 32768:
            return {"status": "worker_failed"}
        result = json.loads(completed.stdout)
        if not isinstance(result, dict) or result.get("status") not in {"ok", "source_error", "schema_changed"}:
            return {"status": "schema_changed"}
        return result
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}
    except (OSError, ValueError, TypeError):
        return {"status": "worker_failed"}


def _write_new(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def _cache_read(path, *, operation, symbol, year, quarter, now, allow_stale=False):
    """Use only a recent, fully bound and hashed provider response."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != "financial-source-v1" or data.get("content_hash") != _digest({k: v for k, v in data.items() if k != "content_hash"}):
        raise ValueError("financial cache schema or content hash mismatch")
    if any(data.get(k) != value for k, value in {"operation": operation, "symbol": symbol, "year": year, "quarter": quarter}.items()):
        raise ValueError("financial cache request mismatch")
    _validate_raw(operation, symbol, year, quarter, data.get("fields"), data.get("rows"))
    if data.get("raw_hash") != _digest({"fields": data["fields"], "rows": data["rows"]}):
        raise ValueError("financial cache raw hash mismatch")
    first, observed = _time(data["first_observed_at"]), _time(data["observed_at"])
    if not first <= observed <= now or (not allow_stale and now - observed > timedelta(hours=24)):
        raise ValueError("financial cache expired or timestamps invalid")
    return data


def _risk_flags(profit, cash):
    flags = []
    if profit:
        if _number(profit["netProfit"]) is not None and _number(profit["netProfit"]) < 0:
            flags.append(NEGATIVE_PROFIT)
        if _number(profit["epsTTM"]) is not None and _number(profit["epsTTM"]) < 0:
            flags.append(NEGATIVE_EPS)
    if cash and _number(cash["CFOToNP"]) is not None and _number(cash["CFOToNP"]) < 0:
        flags.append(NEGATIVE_CASH_RATIO)
    return flags


def collect_financial_review(*, candidates, target_date, cutoff_at, output_directory, cache_directory,
                             max_seconds=120, max_candidates=5, max_queries=20, timeout_seconds=12, online=True):
    """Freeze a small read-only supplement; a provider failure stops this source."""
    all_candidates, target, cutoff = _candidates(candidates), _date(target_date), _time(cutoff_at)
    if cutoff.date() < target:
        raise ValueError("financial cutoff cannot precede target date")
    for value, ceiling in ((max_seconds, 300), (timeout_seconds, 60)):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value <= ceiling:
            raise ValueError("financial time budget must be finite and bounded")
    for value, ceiling in ((max_candidates, 5), (max_queries, 20)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            raise ValueError("financial sample/query budget out of range")
    if not isinstance(online, bool):
        raise ValueError("financial online flag must be boolean")
    output, cache = Path(output_directory), Path(cache_directory)
    started, observed = time.monotonic(), _now()
    records, receipts, count, cache_hits = [], [], 0, 0
    stop = None
    periods = _quarters(target)
    for candidate in all_candidates[:max_candidates]:
        record = {**candidate, "status": "unavailable", "profit": None, "cash_flow": None,
                  "provenance": {"profit": None, "cash_flow": None}, "risk_flags": [], "gaps": []}
        for year, quarter in periods:
            quarter_data = {}
            for operation in FIELDS:
                key = f"{candidate['symbol'].replace('.', '')}-{operation}-{year}Q{quarter}"
                path = cache / "latest" / f"{key}.json"
                raw, cached = None, False
                try:
                    if path.exists():
                        raw = _cache_read(path, operation=operation, symbol=candidate["symbol"], year=year, quarter=quarter, now=observed)
                        cached = True
                except (OSError, ValueError, TypeError, KeyError, OverflowError):
                    record["gaps"].append("财务缓存过期或验证失败，未直接使用该缓存。")
                if raw is None:
                    remaining = max_seconds - (time.monotonic() - started)
                    reason = stop or ("offline" if not online else "query_budget" if count >= max_queries else "runtime_limit" if remaining <= 0 else None)
                    if reason:
                        receipts.append({"symbol": candidate["symbol"], "operation": operation, "year": year, "quarter": quarter, "status": reason, "cache_hit": False})
                        continue
                    count += 1
                    result = _run_query(operation, candidate["symbol"], year, quarter, min(timeout_seconds, remaining))
                    status = result.get("status", "schema_changed") if isinstance(result, dict) else "schema_changed"
                    receipt = {"symbol": candidate["symbol"], "operation": operation, "year": year, "quarter": quarter, "status": status, "cache_hit": False}
                    receipts.append(receipt)
                    if status != "ok":
                        stop = status if status in {"timeout", "source_error", "schema_changed", "worker_failed"} else "source_error"
                        receipt["status"] = stop
                        continue
                    try:
                        _validate_raw(operation, candidate["symbol"], year, quarter, result.get("fields"), result.get("rows"))
                    except (ValueError, TypeError, KeyError, OverflowError):
                        stop = receipt["status"] = "schema_changed"
                        continue
                    fetched = _now().isoformat()
                    body = {"schema_version": "financial-source-v1", "provider": "baostock", "operation": operation,
                            "sdk_version": importlib.metadata.version("baostock"),
                            "symbol": candidate["symbol"], "year": year, "quarter": quarter,
                            "fields": result["fields"], "rows": result["rows"]}
                    body["raw_hash"] = _digest({"fields": body["fields"], "rows": body["rows"]})
                    first = fetched
                    version_path = cache / "versions" / key / f"{body['raw_hash']}.json"
                    try:
                        if version_path.exists():
                            old = _cache_read(version_path, operation=operation, symbol=candidate["symbol"], year=year, quarter=quarter,
                                              now=_now(), allow_stale=True)
                            if _time(old["first_observed_at"]) <= _time(fetched):
                                first = old["first_observed_at"]
                        raw = _seal({**body, "first_observed_at": first, "observed_at": fetched})
                        if not version_path.exists():
                            _write_new(version_path, raw)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
                        _write_new(temporary, raw)
                        temporary.replace(path)
                    except (OSError, ValueError, TypeError, KeyError, OverflowError):
                        raw = _seal({**body, "first_observed_at": fetched, "observed_at": fetched})
                        record["gaps"].append("财务缓存写入或历史版本验证失败，本次仅使用已冻结响应。")
                else:
                    cache_hits += 1
                    receipts.append({"symbol": candidate["symbol"], "operation": operation, "year": year, "quarter": quarter, "status": "ok", "cache_hit": True})
                try:
                    source_file = output / "responses" / f"{key}-{uuid4().hex}.json"
                    _write_new(source_file, raw)
                except OSError:
                    record["gaps"].append("财务响应冻结失败，该响应未进入研究事实。")
                    continue
                receipts[-1].update(raw_hash=raw["raw_hash"], source_file=str(source_file.resolve()))
                row = raw["rows"][0] if raw["rows"] else None
                if row and _date(row["pubDate"]) > min(cutoff.date(), target):
                    receipts[-1]["status"] = "not_yet_published"
                    record["gaps"].append("截止研究日尚未公布的财务记录已排除。")
                    continue
                if row:
                    quarter_data[operation] = (row, {"provider": "baostock", "operation": operation,
                        "first_observed_at": raw["first_observed_at"], "observed_at": raw["observed_at"],
                        "raw_hash": raw["raw_hash"], "pubDate": row["pubDate"], "statDate": row["statDate"],
                        "source_file": str(source_file.resolve()), "historical_reconstruction": _time(raw["first_observed_at"]) > cutoff})
            if quarter_data:
                for operation, (row, provenance) in quarter_data.items():
                    record[operation], record["provenance"][operation] = row, provenance
                break  # Newer partial coverage is explicit; never silently mix quarters.
        present = sum(record[operation] is not None for operation in FIELDS)
        record["status"] = "available" if present == 2 else "partial" if present else "unavailable"
        for operation in FIELDS:
            if record[operation] is not None:
                missing = [field for field in FIELDS[operation][3:] if record[operation][field] == ""]
                if missing:
                    record["gaps"].append(f"{operation} 来源字段为空：{', '.join(missing)}；未补零。")
        if record["profit"] is None:
            record["gaps"].append("未取得研究日可用的季度盈利数据；缺失不表示没有风险。")
        if record["cash_flow"] is None:
            record["gaps"].append("未取得同报告期现金流比率；不推测完整现金流量表。")
        if stop:
            record["gaps"].append("财务数据源发生错误或超时，本轮停止该源的后续网络请求。")
        record["gaps"].append("公司公告、主营业务及财务原文仍待核查。")
        record["gaps"] = list(dict.fromkeys(record["gaps"]))
        record["risk_flags"] = _risk_flags(record["profit"], record["cash_flow"])
        records.append(record)
    status = "empty" if not records else "available" if all(r["status"] == "available" for r in records) else "partial" if any(r["status"] != "unavailable" for r in records) else "unavailable"
    packet = _seal({"schema_version": "financial-review-v1", "target_date": target_date, "cutoff_at": cutoff.isoformat(),
        "observed_at": _now().isoformat(), "status": status, "records": records,
        "network_requests": count, "cache_hits": cache_hits, "request_count_semantics": "financial_sdk_queries_excluding_session_io",
        "receipts": receipts, "limits": {"max_candidates": max_candidates, "max_queries": max_queries, "max_seconds": max_seconds, "timeout_seconds": timeout_seconds},
        "historical_reconstruction": any(p and p["historical_reconstruction"] for r in records for p in r["provenance"].values()),
        "model_calls": 0, "elapsed_seconds": round(time.monotonic() - started, 6), "limitations": LIMITATIONS.copy()})
    validate_financial_review(packet, candidates=all_candidates, target_date=target_date, cutoff_at=cutoff_at)
    try:
        _write_new(output / f"financial-review-{uuid4().hex}.json", packet)
    except OSError:
        packet = _seal({**{k: v for k, v in packet.items() if k != "content_hash"}, "packet_write_failed": True})
    return packet


def validate_financial_review(packet, *, candidates, target_date, cutoff_at=None):
    """Malformed external packets consistently fail closed with ValueError."""
    try:
        _validate_financial_review(packet, candidates=candidates, target_date=target_date, cutoff_at=cutoff_at)
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError("financial review packet malformed") from error


def _validate_financial_review(packet, *, candidates, target_date, cutoff_at=None):
    """Validate a frozen report supplement independently of mutable caches."""
    expected = _candidates(candidates)
    target = _date(target_date)
    if not isinstance(packet, dict) or packet.get("schema_version") != "financial-review-v1" or packet.get("target_date") != target_date:
        raise ValueError("financial review schema/date mismatch")
    if packet.get("content_hash") != _digest({k: v for k, v in packet.items() if k != "content_hash"}):
        raise ValueError("financial review hash mismatch")
    cutoff, observed = _time(packet["cutoff_at"]), _time(packet["observed_at"])
    if cutoff.date() < target or (cutoff_at is not None and cutoff != _time(cutoff_at)):
        raise ValueError("financial review cutoff mismatch")
    limits = packet.get("limits", {})
    cap = limits.get("max_candidates")
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 5:
        raise ValueError("financial review candidate cap invalid")
    if not isinstance(packet.get("records"), list) or len(packet["records"]) != len(expected[:cap]):
        raise ValueError("financial review candidate coverage mismatch")
    for record, identity in zip(packet["records"], expected[:cap], strict=True):
        if not isinstance(record, dict) or not isinstance(record.get("provenance"), dict) or set(record["provenance"]) != set(FIELDS):
            raise ValueError("financial review record/provenance malformed")
        if any(record.get(k) != identity[k] for k in ("security_id", "symbol")):
            raise ValueError("financial review identity mismatch")
        quarters = []
        for operation in FIELDS:
            row = record.get(operation)
            provenance = record.get("provenance", {}).get(operation)
            if row is None:
                if provenance is not None:
                    raise ValueError("financial review has provenance without data")
                continue
            if not isinstance(provenance, dict):
                raise ValueError("financial review missing provenance")
            stat = _date(row["statDate"])
            year, quarter = stat.year, (stat.month + 2) // 3
            _validate_raw(operation, identity["symbol"], year, quarter, FIELDS[operation], [row])
            if (year, quarter) not in _quarters(target) or _date(row["pubDate"]) > min(cutoff.date(), target):
                raise ValueError("financial review future or unsupported period")
            if provenance.get("provider") != "baostock" or provenance.get("operation") != operation or any(provenance.get(k) != row[k] for k in ("pubDate", "statDate")):
                raise ValueError("financial review source binding mismatch")
            if provenance.get("raw_hash") != _digest({"fields": FIELDS[operation], "rows": [row]}):
                raise ValueError("financial review raw hash mismatch")
            first, fetched = _time(provenance["first_observed_at"]), _time(provenance["observed_at"])
            if not first <= fetched <= observed or provenance.get("historical_reconstruction") is not (first > cutoff):
                raise ValueError("financial review observation time mismatch")
            if not isinstance(provenance.get("source_file"), str) or not provenance["source_file"]:
                raise ValueError("financial review missing frozen source")
            quarters.append(stat)
        if len(set(quarters)) > 1:
            raise ValueError("financial review mixes reporting periods")
        expected_status = "available" if len(quarters) == 2 else "partial" if quarters else "unavailable"
        if record.get("status") != expected_status or record.get("risk_flags") != _risk_flags(record.get("profit"), record.get("cash_flow")):
            raise ValueError("financial review status/risk derivation mismatch")
        if not isinstance(record.get("gaps"), list) or len(record["gaps"]) > 20 or any(not isinstance(gap, str) or not 1 <= len(gap) <= 1000 for gap in record["gaps"]):
            raise ValueError("financial review gaps malformed")
    records = packet["records"]
    status = "empty" if not records else "available" if all(r["status"] == "available" for r in records) else "partial" if any(r["status"] != "unavailable" for r in records) else "unavailable"
    if packet.get("status") != status or packet.get("model_calls") != 0 or packet.get("limitations") != LIMITATIONS:
        raise ValueError("financial review status or limitations invalid")
    for key in ("network_requests", "cache_hits"):
        if isinstance(packet.get(key), bool) or not isinstance(packet.get(key), int) or packet[key] < 0:
            raise ValueError("financial review request count invalid")
    if packet["network_requests"] > limits.get("max_queries", -1) or not 1 <= limits.get("max_queries", 0) <= 20:
        raise ValueError("financial review query budget exceeded")
    if isinstance(limits.get("max_queries"), bool) or not isinstance(limits.get("max_queries"), int):
        raise ValueError("financial review query limit invalid")
    for key, ceiling in (("max_seconds", 300), ("timeout_seconds", 60)):
        value = limits.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= ceiling:
            raise ValueError("financial review runtime limit invalid")
    if packet.get("historical_reconstruction") is not any(p and p["historical_reconstruction"] for r in records for p in r["provenance"].values()):
        raise ValueError("financial review historical reconstruction flag mismatch")
    elapsed = packet.get("elapsed_seconds")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("financial review elapsed time invalid")


def _worker():
    """Ignore SDK logs/errors; only a fixed public response schema leaves worker."""
    result = {"status": "source_error"}
    try:
        request = json.loads(sys.stdin.read(4096))
        if set(request) != {"operation", "symbol", "year", "quarter"} or request["operation"] not in FIELDS or not re.fullmatch(r"(?:sh|sz)\.\d{6}", request["symbol"]) or not isinstance(request["year"], int) or not 1990 <= request["year"] <= 2200 or request["quarter"] not in (1, 2, 3, 4):
            raise ValueError("invalid financial worker request")
        # SDK logs never reach logs, report, or stderr; no credentials accepted.
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            import baostock as bs
            from .providers.baostock_worker import read_rows
            login = bs.login()
            if getattr(login, "error_code", None) == "0":
                try:
                    query = bs.query_profit_data if request["operation"] == "profit" else bs.query_cash_flow_data
                    response = query(code=request["symbol"], year=request["year"], quarter=request["quarter"])
                    fields, rows = read_rows(response, FIELDS[request["operation"]])
                    _validate_raw(request["operation"], request["symbol"], request["year"], request["quarter"], fields, rows)
                    result = {"status": "ok", "fields": fields, "rows": rows}
                finally:
                    bs.logout()
    except Exception:
        # Intentional boundary: SDK exceptions can contain server/auth details.
        result = {"status": "source_error"}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker()
