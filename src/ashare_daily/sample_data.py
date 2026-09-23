"""M2.1 bounded, configured sample collection using the existing M1/M2 adapters.

The pool is a reproducible technical test fixture, not a market universe. A
failed symbol stays in the configured sample; no return-dependent replacement
or implicit eligibility decision is permitted here.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import csv
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4

from ashare_daily.m2_data import canonical_hash, normalize_adjusted_response, prepare_adjusted_data
from ashare_daily.market import STOP_SOURCE_STATUSES, csv_text, incremental_windows, validate_paths
from ashare_daily.providers.baostock import BaoStockClient, SHANGHAI
from ashare_daily.quality.baostock import normalize_bars, normalize_calendar, normalize_instrument
from ashare_daily.storage.market import MarketStore


def _write(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def load_sample_config(path: Path | str) -> dict:
    """Validate the explicit pool and recompute its price-independent selection."""
    content = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(content, dict):
        raise ValueError("M2.1 样本配置必须是 JSON 对象")
    if content.get("schema_version") != "m21-fixed-samples-v1":
        raise ValueError("不支持的 M2.1 样本配置版本")
    if not isinstance(content.get("sample_id"), str) or not content["sample_id"]:
        raise ValueError("样本配置必须有 sample_id")
    count = content.get("stock_count")
    if isinstance(count, bool) or count not in (6, 30, 100):
        raise ValueError("M2.1 仅允许明确的 6、30 或 100 只股票配置")
    if not isinstance(content.get("selection_method"), dict) or content["selection_method"].get("algorithm") != "preserve_then_id_ascending_equal_exchanges_v1":
        raise ValueError("不支持的固定样本选择方法")
    pools, preserved = content.get("candidate_pools", {}), content.get("preserve_symbols", [])
    if (not isinstance(pools, dict) or set(pools) != {"sh", "sz"} or not isinstance(preserved, list)
            or any(not isinstance(symbol, str) for symbol in preserved) or len(preserved) != len(set(preserved))):
        raise ValueError("候选池或保留样本配置无效")
    selected = []
    all_pool = []
    for exchange in ("sh", "sz"):
        pool = pools[exchange]
        if (not isinstance(pool, list) or any(not isinstance(symbol, str) for symbol in pool)
                or len(pool) != len(set(pool)) or pool != sorted(pool)):
            raise ValueError("各交易所代码池必须唯一且按证券 ID 排序")
        pattern = r"sh\.(?:600|601|603|605)\d{3}" if exchange == "sh" else r"sz\.(?:000|001|002|003)\d{3}"
        if any(not isinstance(symbol, str) or re.fullmatch(pattern, symbol) is None for symbol in pool):
            raise ValueError("配置代码不在已批准的沪深主板 A 股代码格式范围；仍需主数据核验")
        keep = sorted(symbol for symbol in preserved if symbol.startswith(exchange + "."))
        if any(symbol not in pool for symbol in keep) or len(keep) > count // 2 or len(pool) < count // 2:
            raise ValueError("保留代码不在池内或代码池不足")
        selected.extend(sorted([*keep, *[symbol for symbol in pool if symbol not in keep][:count // 2 - len(keep)]]))
        all_pool.extend(pool)
    if any(symbol not in all_pool for symbol in preserved):
        raise ValueError("保留样本包含非目标证券")
    benchmark = content.get("benchmark", {})
    if benchmark != {"symbol": "sh.000001", "name": "上证指数"}:
        raise ValueError("本轮沿用现有 sh.000001 上证指数基准")
    expected = {benchmark["symbol"]: "index", **{symbol: "stock" for symbol in sorted(selected)}}
    if content.get("symbol_types") != expected:
        raise ValueError("样本清单与声明的确定性选择方法不一致")
    content["config_path"] = str(Path(path).resolve())
    content["config_hash"] = canonical_hash({key: value for key, value in content.items() if key not in {"config_path", "config_hash"}})
    return content


def _missing_windows(trading_dates: list[date], stored: set[date]) -> list[tuple[date, date]]:
    """Reuse M1 grouping but omit its recent refresh tail for a fixed historical T."""
    missing = set(trading_dates) - stored
    return [(start, end) for start, end in incremental_windows(trading_dates, stored)
            if any(start <= day <= end for day in missing)]


def _reuse_adjusted(roots: list[Path], symbol_types: dict[str, str], trading_dates: list[date], *, live: bool) -> tuple[dict, list[dict]]:
    """Reuse only a whole per-symbol response for this exact dependency window."""
    result, references = {}, []
    from ashare_daily.operations.paths import resolve_archived_path
    paths = sorted({path for root in roots if root.exists() for path in root.glob("**/manifest.json")}, reverse=True)
    for path in paths:
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
            content = {key: value for key, value in bundle.items() if key != "manifest_hash"}
            if (bundle.get("schema_version") != "m2-adjusted-bundle-v1" or bundle.get("manifest_hash") != canonical_hash(content)
                    or bundle.get("trading_dates") != [day.isoformat() for day in trading_dates]
                    or (live and bundle.get("verification_kind") != "live_network")
                    or (not live and bundle.get("verification_kind") != "offline_test")):
                continue
            reused = []
            for symbol, original in bundle.get("series", {}).items():
                if symbol not in symbol_types or symbol in result:
                    continue
                try:
                    response_path = resolve_archived_path(original["response_path"], anchor=path)
                    envelope = json.loads(response_path.read_text(encoding="utf-8"))
                    if envelope.get("verification_kind") != bundle["verification_kind"]:
                        continue
                    verified = normalize_adjusted_response(envelope["result"], symbol=symbol, security_type=symbol_types[symbol], trading_dates=trading_dates)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if any(original.get(key) != value for key, value in verified.items()):
                    continue
                result[symbol] = deepcopy(original)
                reused.append(symbol)
            if reused:
                references.append({"manifest_path": str(path.resolve()), "manifest_hash": bundle["manifest_hash"], "symbols": reused})
        except (OSError, ValueError, KeyError, TypeError):
            # Corrupt caches are not treated as empty supplier responses or data.
            continue
    return result, references


def collect_sample_data(
    sample_config: Path | str | dict, *, target_date: date,
    database: Path = Path("data/research/market.sqlite3"),
    output_dir: Path = Path("outputs/research/m21_collection"),
    adjusted_dir: Path = Path("data/research/m21_adjusted"),
    reuse_adjusted_dirs: list[Path] | None = None,
    history_points: int = 120, timeout_seconds: float = 20, max_attempts: int = 2,
    client: Any = None, allow_current_day: bool = False, refresh_master_days: int | None = None,
) -> dict:
    """Collect at most 100 configured stocks plus one benchmark; preserve evidence."""
    config = load_sample_config(sample_config) if not isinstance(sample_config, dict) else load_sample_config(sample_config["config_path"])
    current = datetime.now(SHANGHAI)
    if type(target_date) is not date or target_date > current.date() or (target_date == current.date() and
            (not allow_current_day or current.hour < 16)):
        raise ValueError("M2.1 扩展验证要求明确指定已完成的历史交易日")
    if type(history_points) is not int or not 120 <= history_points <= 260:
        raise ValueError("历史窗口必须为 120 至 260 个交易日")
    validate_paths(Path(database), Path(output_dir))
    validate_paths(Path(database), Path(adjusted_dir))
    client = client if client is not None else BaoStockClient(timeout_seconds=timeout_seconds, max_attempts=max_attempts)
    live = type(client) is BaoStockClient
    if not live:
        project = Path(__file__).resolve().parents[2]
        for path in (Path(database).resolve(), Path(output_dir).resolve(), Path(adjusted_dir).resolve()):
            if any(path == root or root in path.parents for root in (project / "data" / "research", project / "outputs" / "research")):
                raise ValueError("离线客户端禁止写入真实 research 路径")
    now = datetime.now(SHANGHAI)
    run_id = now.strftime("%Y%m%dT%H%M%S%f") + "-" + config["sample_id"] + "-" + uuid4().hex[:8]
    directory = Path(output_dir).resolve() / run_id
    (directory / "requests").mkdir(parents=True, exist_ok=False)
    result = {"schema_version": "m21-collection-v1", "status": "failed", "run_id": run_id,
              "run_directory": str(directory), "mode": "research" if live else "offline_test",
              "verification_kind": "live_network" if live else "offline_test",
              "scope": "扩展样本试运行版；技术样本，非推荐，不代表全市场", "sample_config": config,
              "configured_stock_count": config["stock_count"], "target_trade_date": target_date.isoformat(),
              "created_at": now.isoformat(), "database_path": str(Path(database).resolve()),
              "items": [], "requests": [], "failures": []}
    store = MarketStore(database)
    # Test isolation must survive a later process, not only this call's client
    # type. The additive marker leaves the M1 schema/version contract intact.
    with sqlite3.connect(store.path) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM market_metadata"))
        kind = metadata.get("verification_kind")
        if live and kind == "offline_test":
            raise ValueError("真实采集拒绝复用含 offline_test 标记的数据库")
        if not live:
            if kind == "live_network" or (kind is None and store.row_count() > 0):
                raise ValueError("离线采集拒绝覆盖已有真实或未标注来源的数据")
            connection.execute("INSERT OR REPLACE INTO market_metadata(key,value) VALUES ('verification_kind','offline_test')")
        elif kind is None:
            connection.execute("INSERT INTO market_metadata(key,value) VALUES ('verification_kind','live_network')")
    result["database_rows_before"] = store.row_count()
    blocked: str | None = None
    consecutive_failures = 0

    def query(operation: str, **parameters: str) -> dict:
        nonlocal blocked, consecutive_failures
        response = client.query(operation, **parameters)
        path = directory / "requests" / f"{len(result['requests']) + 1:03d}-{operation}.json"
        _write(path, {"verification_kind": result["verification_kind"], "result": response})
        result["requests"].append({"path": str(path), "operation": operation, "parameters": parameters,
                                   "ok": response.get("ok"), "status": response.get("status"),
                                   "row_count": len(response.get("rows", [])), "fetched_at": response.get("fetched_at")})
        consecutive_failures = 0 if response.get("ok") else consecutive_failures + 1
        if response.get("status") in STOP_SOURCE_STATUSES or consecutive_failures >= 3:
            blocked = f"来源停止：{response.get('status')}；连续失败 {consecutive_failures} 次"
        return response

    def finish() -> dict:
        if not result["items"]:
            reason = result["failures"][0]["reason"] if result["failures"] else "前置条件未完成"
            result["items"] = [{"symbol": symbol, "status": "not_attempted", "reason": reason,
                                "inserted": 0, "updated": 0, "unchanged": 0, "unadjusted_rows": 0, "adjusted_rows": 0}
                               for symbol in config["symbol_types"]]
        result["finished_at"] = datetime.now(SHANGHAI).isoformat()
        result["database_rows_after"] = store.row_count()
        result["network_request_count"] = len(result["requests"]) + result.get("adjusted_network_request_count", 0)
        result["stock_market_success_count"] = sum(item["status"] == "ok" and config["symbol_types"][item["symbol"]] == "stock" for item in result["items"])
        result["stock_market_returned_count"] = sum(item.get("unadjusted_rows", 0) > 0 and config["symbol_types"][item["symbol"]] == "stock" for item in result["items"])
        result["stock_market_full_window_count"] = sum(item.get("unadjusted_rows", 0) == history_points and config["symbol_types"][item["symbol"]] == "stock" for item in result["items"])
        result["market_success_count"] = sum(item["status"] == "ok" for item in result["items"])
        result["failure_count"] = sum(item["status"] not in {"ok", "not_attempted"} for item in result["items"])
        result["not_attempted_count"] = sum(item["status"] == "not_attempted" for item in result["items"])
        result["manifest_hash"] = canonical_hash(result)
        _write(directory / "result.json", result)
        with (directory / "sample_status.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            columns = ["scope", "symbol", "name", "status", "reason", "unadjusted_rows", "adjusted_rows", "inserted", "updated", "unchanged"]
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for item in result["items"]:
                writer.writerow({**item, "scope": result["scope"], "reason": csv_text(item.get("reason", ""))})
        return result

    floor = target_date - timedelta(days=365)
    calendar = store.calendar_dates(floor, target_date)
    expected_calendar = {floor + timedelta(days=n) for n in range(366)}
    if set(calendar) != expected_calendar:
        response = query("calendar", start_date=floor.isoformat(), end_date=target_date.isoformat())
        try:
            if not response.get("ok"):
                raise ValueError(response.get("error_msg", "日历请求失败"))
            days = normalize_calendar(response["rows"], start_date=floor, end_date=target_date,
                                      fetched_at=datetime.fromisoformat(response["fetched_at"]), sdk_version=response["sdk_version"])
            store.store_calendar(days)
            calendar = {day.calendar_date: day.is_trading_day for day in days}
        except (ValueError, KeyError, TypeError) as exc:
            result["failures"].append({"stage": "calendar", "reason": str(exc)})
            return finish()
    trading_dates = sorted(day for day, trading in calendar.items() if trading)[-history_points:]
    if calendar.get(target_date) is not True or len(trading_dates) != history_points:
        result["failures"].append({"stage": "calendar", "reason": "目标日不是已确认交易日，或有效交易日历不足；不改变窗口"})
        return finish()
    start = trading_dates[0]
    result.update(start_date=start.isoformat(), end_date=target_date.isoformat(), trading_dates=[day.isoformat() for day in trading_dates])
    for symbol, kind in sorted(config["symbol_types"].items()):
        item = {"symbol": symbol, "status": "failed", "inserted": 0, "updated": 0, "unchanged": 0,
                "unadjusted_rows": 0, "adjusted_rows": 0, "requested_windows": []}
        result["items"].append(item)
        if blocked:
            item.update(status="not_attempted", reason=blocked)
            continue
        try:
            instrument = store.get_instrument(symbol)
            if instrument is None or (refresh_master_days is not None and
                    (now - instrument.fetched_at).total_seconds() >= refresh_master_days * 86400):
                response = query("basic", code=symbol)
                if not response.get("ok") or len(response.get("rows", [])) != 1:
                    raise ValueError("基础信息失败或不唯一：" + response.get("error_msg", "empty_response"))
                instrument = normalize_instrument(response["rows"][0], expected_symbol=symbol, expected_type=kind,
                                                  fetched_at=datetime.fromisoformat(response["fetched_at"]), sdk_version=response["sdk_version"])
                store.store_instrument(instrument)
            if instrument.security_type != kind or instrument.board != ("mainboard" if kind == "stock" else "index"):
                raise ValueError("证券主数据类型与固定样本范围不符")
            if (instrument.ipo_date and instrument.ipo_date > target_date) or (instrument.out_date and instrument.out_date < target_date):
                raise ValueError("基础信息生效区间不覆盖分析日；保留该失败样本，不替换")
            item["name"] = instrument.name
            bars = store.read_bars([symbol], start, target_date)
            complete = {bar.trade_date for bar in bars if not bar.quality_flags}
            if target_date not in complete:
                response = query("history", code=symbol, security_type=kind, start_date=target_date.isoformat(), end_date=target_date.isoformat(), adjustment_mode="unadjusted")
                if not response.get("ok"):
                    raise ValueError("单日探测失败：" + response.get("error_msg", "provider_failure"))
                probe = normalize_bars(response["rows"], instrument=instrument, start_date=target_date, end_date=target_date,
                                       trading_dates={target_date}, fetched_at=datetime.fromisoformat(response["fetched_at"]), sdk_version=response["sdk_version"])
                if len(probe) != 1:
                    raise ValueError("单日响应为空或日期不符")
                for key, value in store.store_bars(probe).items():
                    item[key] += value
                if not probe[0].quality_flags:
                    complete.add(target_date)
            for begin, end in _missing_windows(trading_dates, complete)[:8]:
                if blocked:
                    raise ValueError(blocked)
                item["requested_windows"].append([begin.isoformat(), end.isoformat()])
                response = query("history", code=symbol, security_type=kind, start_date=begin.isoformat(), end_date=end.isoformat(), adjustment_mode="unadjusted")
                if not response.get("ok"):
                    raise ValueError("历史请求失败：" + response.get("error_msg", "provider_failure"))
                values = normalize_bars(response["rows"], instrument=instrument, start_date=begin, end_date=end,
                                        trading_dates=set(trading_dates), fetched_at=datetime.fromisoformat(response["fetched_at"]), sdk_version=response["sdk_version"])
                if not values:
                    raise ValueError("历史响应为空；不以空集合视为完整")
                for key, value in store.store_bars(values).items():
                    item[key] += value
            saved = store.read_bars([symbol], start, target_date)
            item["unadjusted_rows"] = len(saved)
            missing = set(trading_dates) - {bar.trade_date for bar in saved if not bar.quality_flags}
            if missing:
                raise ValueError(f"未复权窗口缺少 {len(missing)} 条有效日线；历史不足或源数据缺口，不填充")
            item["status"] = "ok"
        except (ValueError, KeyError, TypeError) as exc:
            item.update(status="failed", reason=str(exc))
            result["failures"].append({"symbol": symbol, "stage": "unadjusted", "reason": str(exc)})
    roots = [Path(adjusted_dir), *(reuse_adjusted_dirs if reuse_adjusted_dirs is not None else [Path("data/research/m2_adjusted")])]
    series, references = _reuse_adjusted(roots, config["symbol_types"], trading_dates, live=live)
    pending = {item["symbol"]: config["symbol_types"][item["symbol"]] for item in result["items"] if item["unadjusted_rows"] > 0 and item["symbol"] not in series}
    bundle_failures, batches = [], []
    for offset in range(0, len(pending), 10):
        if blocked:
            break
        batch_types = dict(list(pending.items())[offset:offset + 10])
        batch = prepare_adjusted_data(batch_types, trading_dates, Path(adjusted_dir), client=client)
        batches.append({"manifest_path": str(Path(batch["run_directory"]) / "manifest.json"), "manifest_hash": batch["manifest_hash"], "symbols": list(batch_types)})
        series.update(batch["series"])
        bundle_failures.extend(batch["failures"])
        result["adjusted_network_request_count"] = result.get("adjusted_network_request_count", 0) + len(list((Path(batch["run_directory"]) / "responses").glob("*.json")))
        if any(failure["status"] in STOP_SOURCE_STATUSES for failure in batch["failures"]):
            blocked = "调整数据源权限、限流或结构异常，停止后续批次"
        elif len(batch["failures"]) == len(batch_types) and not batch["series"]:
            blocked = "整批调整数据获取失败，停止扩大请求；可保留已有成果后人工重跑"
    for item in result["items"]:
        symbol = item["symbol"]
        item["adjusted_rows"] = len(series.get(symbol, {}).get("bars", []))
        if series.get(symbol, {}).get("issues") and not any(failure.get("symbol") == symbol for failure in bundle_failures):
            bundle_failures.append({"symbol": symbol, "status": "partial", "reason": "; ".join(series[symbol]["issues"])})
        if symbol not in series or series[symbol].get("issues"):
            if item["status"] == "ok":
                item.update(status="failed", reason=blocked or "调整数据缺失或存在质量问题")
            if not any(failure.get("symbol") == symbol for failure in bundle_failures):
                bundle_failures.append({"symbol": symbol, "status": "missing", "reason": item.get("reason", "调整数据未取得")})
    bundle_dir = Path(adjusted_dir).resolve() / (run_id + "-combined")
    bundle_dir.mkdir(parents=True, exist_ok=False)
    bundle = {"schema_version": "m2-adjusted-bundle-v1", "batch_id": run_id + "-combined",
              "mode": result["mode"], "verification_kind": result["verification_kind"], "scope": result["scope"],
              "created_at": datetime.now(SHANGHAI).isoformat(), "run_directory": str(bundle_dir),
              "start_date": start.isoformat(), "end_date": target_date.isoformat(), "trading_dates": result["trading_dates"],
              "symbol_types": config["symbol_types"], "series": series, "failures": bundle_failures,
              "component_manifests": batches, "reused_manifests": references,
              "adjustment_consistency": "Each symbol reuses or fetches one complete response for the exact window. No cross-response price stitching; supplier-wide atomic adjustment version unavailable.",
              "delisting_period_status": "unknown", "delisting_period_reason": "行情读取不提供退市整理期证据；由独立资格流程核验。"}
    bundle["success_count"] = sum(not item.get("issues") for item in series.values())
    bundle["failure_count"] = len(bundle_failures)
    bundle["status"] = "ok" if not bundle_failures else ("partial" if series else "failed")
    bundle["manifest_hash"] = canonical_hash(bundle)
    _write(bundle_dir / "manifest.json", bundle)
    result.update(adjusted_manifest=str(bundle_dir / "manifest.json"), adjusted_manifest_hash=bundle["manifest_hash"],
                  adjusted_reused_series_count=sum(len(item["symbols"]) for item in references), adjusted_new_batches=batches)
    result["coverage"] = store.coverage(config["symbol_types"], start, target_date, target_date)
    result["status"] = "ok" if all(item["status"] == "ok" for item in result["items"]) else "partial"
    result["failures"].extend({**failure, "stage": "adjusted"} for failure in bundle_failures)
    store.export_csv(directory / "sample_bars.csv", config["symbol_types"], start, target_date)
    return finish()
