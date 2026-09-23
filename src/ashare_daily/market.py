"""M1 only: bounded real-data probes, incremental ingestion and local coverage."""

from datetime import date, datetime, timedelta
from importlib.metadata import version
from importlib import import_module
import csv
import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from ashare_daily.m1_settings import (
    DEFAULT_DATABASE, DEFAULT_EVIDENCE_DIR, EARLIEST_DAILY_CHECK, MAX_HISTORY_DAYS,
    MAX_WINDOWS_PER_SYMBOL, OVERLAP_TRADING_DAYS, SAMPLE_ID, SAMPLE_TYPES, SCOPE,
)
from ashare_daily.providers.baostock import BaoStockClient
from ashare_daily.quality.baostock import normalize_bars, normalize_calendar, normalize_instrument
from ashare_daily.storage.market import MarketStore

SHANGHAI = ZoneInfo("Asia/Shanghai")
STOP_SOURCE_STATUSES = {"permission_denied", "rate_limited", "schema_changed"}


def write_json(path: Path, content: object) -> None:
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def csv_text(value: object) -> str:
    """Keep a remote error message as text when the diagnostic CSV opens in Excel."""
    text = str(value or "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def validate_paths(database: Path, output_dir: Path) -> None:
    for path in (database, output_dir):
        if any(part.lower() in {"demo", "fixtures"} for part in path.resolve().parts):
            raise ValueError("M1 真实数据路径不能位于 demo 或 fixtures 目录")


def incremental_windows(trading_dates: list[date], stored: set[date], *, refresh: bool = False) -> list[tuple[date, date]]:
    """Request holes and a small overlapping tail, not an entire history each day."""
    ordered = sorted(set(trading_dates))
    if not ordered:
        return []
    wanted = set(ordered) if refresh else (set(ordered) - stored) | set(ordered[-OVERLAP_TRADING_DAYS:])
    windows = []
    start = end = None
    for day in ordered:
        if day in wanted:
            if start is None:
                start = day
            end = day
        elif start is not None:
            windows.append((start, end))
            start = end = None
    if start is not None:
        windows.append((start, end))
    return windows


class M1Run:
    def __init__(self, operation: str, output_dir: Path, client, now: datetime):
        self.client = client
        self.now = now
        self.run_id = f"{now:%Y%m%dT%H%M%S%f}-{operation}-{uuid4().hex[:8]}"
        self.directory = output_dir.resolve() / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        (self.directory / "requests").mkdir()
        self.requests = []
        self.result = {
            "run_id": self.run_id, "operation": operation, "mode": "research",
            "verification_kind": "live_network" if isinstance(client, BaoStockClient) else "offline_test",
            "scope": SCOPE, "sample_id": SAMPLE_ID, "sample_symbols": list(SAMPLE_TYPES),
            "provider": "baostock", "sdk_version": version("baostock"),
            "actual_generated_at": now.isoformat(), "timezone": "Asia/Shanghai",
            "status": "failed", "target_trade_date": None, "actual_latest_data_date": None,
            "adjustment_mode": "unadjusted", "items": [], "failures": [],
            "endpoint": "public-api.baostock.com:10030 (SDK default)",
            "notice": "本地技术验证数据；未生成选股、新闻分析或研究日报。无模拟数据替补。",
        }
        try:
            import_module("baostock")
            self.result["sdk_importable"] = True
        except ImportError:
            self.result["sdk_importable"] = False

    def query(self, operation, **parameters):
        response = self.client.query(operation, **parameters)
        number = len(self.requests) + 1
        filename = f"{number:03d}-{operation}.json"
        write_json(self.directory / "requests" / filename, response)
        self.requests.append({
            "request_file": f"requests/{filename}", "operation": operation,
            "parameters": parameters, "ok": response["ok"], "status": response["status"],
            "error_code": response.get("error_code"), "error_msg": response.get("error_msg"),
            "row_count": len(response.get("rows", [])), "fetched_at": response.get("fetched_at"),
            "elapsed_seconds": response.get("elapsed_seconds"), "login": response.get("login"),
            "attempt_count": len(response.get("attempts", [])),
        })
        return response

    def fail(self, stage, message, *, symbol=None, status="failed"):
        self.result["failures"].append({"stage": stage, "symbol": symbol, "status": status, "reason": str(message)})

    def finish(self):
        self.result["requests"] = self.requests
        self.result["finished_at"] = datetime.now(SHANGHAI).isoformat()
        self.result["run_directory"] = str(self.directory)
        self.result["success_count"] = sum(item.get("status") == "ok" for item in self.result["items"])
        self.result["failure_count"] = sum(item.get("status") not in {"ok", "not_attempted"} for item in self.result["items"])
        self.result["not_attempted_count"] = len(SAMPLE_TYPES) - self.result["success_count"] - self.result["failure_count"]
        self.result["login_success"] = any((request.get("login") or {}).get("ok") for request in self.requests)
        self.result["calendar_request_success"] = any(request["operation"] == "calendar" and request["ok"] for request in self.requests)
        self.result["target_data_verified"] = bool(self.result.get("single_day_probe_passed"))
        self.result["network_request_count"] = len(self.requests)
        write_json(self.directory / "result.json", self.result)
        # A symbol-level CSV remains useful even if the source is unavailable.
        with (self.directory / "sample_status.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["scope", "symbol", "expected_type", "status", "rows", "latest_date", "reason"])
            writer.writeheader()
            by_symbol = {item["symbol"]: item for item in self.result["items"]}
            for symbol, expected_type in SAMPLE_TYPES.items():
                item = by_symbol.get(symbol, {})
                writer.writerow({"scope": "小样本验证（非推荐）", "symbol": symbol, "expected_type": expected_type,
                                 "status": item.get("status", "not_attempted"), "rows": item.get("rows", 0),
                                 "latest_date": item.get("latest_date"),
                                 "reason": csv_text(item.get("reason", "前置检查未通过" if self.result["failures"] else ""))})
        latest_path = self.directory.parent / f"latest_{self.result['operation']}.json"
        temporary = self.directory.parent / f".latest-{self.run_id}.tmp"
        write_json(temporary, {"result_file": str(self.directory / "result.json"), "run_directory": str(self.directory), "status": self.result["status"]})
        temporary.replace(latest_path)
        return self.result


def _fetched(response) -> datetime:
    return datetime.fromisoformat(response["fetched_at"])


def _calendar(run: M1Run, start: date, end: date):
    response = run.query("calendar", start_date=start.isoformat(), end_date=end.isoformat())
    if not response["ok"]:
        run.fail("calendar", response["error_msg"], status=response["status"])
        return None
    try:
        return normalize_calendar(response["rows"], start_date=start, end_date=end,
                                  fetched_at=_fetched(response), sdk_version=response["sdk_version"])
    except ValueError as exc:
        run.fail("calendar_validation", exc, status="schema_changed")
        return None


def _target(run: M1Run, requested: date | None, days) -> date | None:
    trading = [item.calendar_date for item in days if item.is_trading_day]
    if requested is not None:
        if requested not in trading:
            run.result.update(status="non_trading_day", requested_date=requested.isoformat())
            return None
        target = requested
    else:
        completed = [day for day in trading if day < run.now.date() or run.now.time().replace(tzinfo=None) >= EARLIEST_DAILY_CHECK]
        if not completed:
            run.fail("target_date", "获取的交易日历中没有可验证的已完成交易日")
            return None
        target = max(completed)
    run.result["target_trade_date"] = target.isoformat()
    if target == run.now.date() and run.now.time().replace(tzinfo=None) < EARLIEST_DAILY_CHECK:
        run.result["status"] = "not_ready"
        run.fail("target_date", "尚未到北京时间 18:15 日线检查时间，不能把旧数据或盘中数据当作当日日线；实际最新数据日未查询。", status="not_ready")
        return None
    return target


def _probe(run: M1Run, target: date):
    """Exactly one completed trading day must pass before historical retrieval."""
    instruments = {}
    bars = []
    blocked = False
    for symbol, expected_type in SAMPLE_TYPES.items():
        item = {"symbol": symbol, "expected_type": expected_type, "status": "failed", "rows": 0, "latest_date": None}
        run.result["items"].append(item)
        if blocked:
            item.update(status="not_attempted", reason="数据源已返回权限或访问限制，停止后续请求")
            continue
        basic = run.query("basic", code=symbol)
        if not basic["ok"]:
            item.update(status=basic["status"], reason=basic["error_msg"])
            blocked = basic["status"] in STOP_SOURCE_STATUSES
            continue
        try:
            if len(basic["rows"]) != 1:
                raise ValueError("证券基本信息必须唯一且非空")
            instrument = normalize_instrument(basic["rows"][0], expected_symbol=symbol, expected_type=expected_type,
                                              fetched_at=_fetched(basic), sdk_version=basic["sdk_version"])
            if instrument.status != "listed":
                raise ValueError("技术样本的证券基础状态不是上市，不继续采集")
            if instrument.security_type == "stock" and instrument.ipo_date is None:
                raise ValueError("股票样本缺少上市日期，不猜测历史覆盖资格")
            instruments[symbol] = instrument
            response = run.query("history", code=symbol, start_date=target.isoformat(), end_date=target.isoformat(),
                                 security_type=expected_type, adjustment_mode="unadjusted")
            raw_dates = [str(row.get("date", "")) for row in response.get("rows", []) if row.get("date")]
            item["latest_date"] = max(raw_dates, default=None)
            if not response["ok"]:
                item.update(status=response["status"], reason=response["error_msg"])
                blocked = response["status"] in STOP_SOURCE_STATUSES
                continue
            normalized = normalize_bars(response["rows"], instrument=instrument, start_date=target, end_date=target,
                                         trading_dates={target}, fetched_at=_fetched(response), sdk_version=response["sdk_version"])
            if len(normalized) != 1:
                item.update(status="stale", reason="目标交易日没有返回唯一有效日线；不能视为今日数据")
                continue
            bar = normalized[0]
            if expected_type == "stock" and (bar.is_st is not False or bar.tradestatus is not True):
                raise ValueError("单日技术样本未满足普通非 ST、正常交易的要求")
            if bar.quality_flags:
                raise ValueError("单日样本存在数据质量缺口：" + "; ".join(bar.quality_flags))
            bars.extend(normalized)
            item.update(status="ok", rows=1, latest_date=target.isoformat(), name=instrument.name, reason="单日数据及证券类型校验通过")
        except ValueError as exc:
            item.update(status="validation_failed", reason=str(exc))
    run.result["actual_latest_data_date"] = max((item["latest_date"] for item in run.result["items"] if item["latest_date"]), default=None)
    passed = len(bars) == len(SAMPLE_TYPES)
    run.result["single_day_probe_passed"] = passed
    if not passed:
        for item in run.result["items"]:
            if item["status"] != "ok":
                run.fail("single_day_probe", item.get("reason", "单日样本未通过"), symbol=item["symbol"], status=item["status"])
    return instruments, bars, passed


def run_market(operation: str, *, target_date: date | None = None, start_date: date | None = None,
               database: Path = DEFAULT_DATABASE, output_dir: Path = DEFAULT_EVIDENCE_DIR,
               refresh: bool = False, timeout_seconds: float = 20, max_attempts: int = 2,
               client=None, now: datetime | None = None) -> dict:
    if operation not in {"doctor", "collect"}:
        raise ValueError("未知 M1 操作")
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        raise ValueError("业务时间必须带时区")
    now = now.astimezone(SHANGHAI)
    if target_date and target_date > now.date():
        raise ValueError("不能获取未来交易日的数据")
    if start_date and operation != "collect":
        raise ValueError("doctor 只验证一个交易日")
    if start_date and target_date and (start_date > target_date or (target_date - start_date).days > MAX_HISTORY_DAYS):
        raise ValueError("采集区间需为正向且最多 366 天；M1 仅约一年小样本")
    validate_paths(database, output_dir)
    if client is not None and not isinstance(client, BaoStockClient):
        if database.resolve() == DEFAULT_DATABASE.resolve() or output_dir.resolve() == DEFAULT_EVIDENCE_DIR.resolve():
            raise ValueError("离线注入客户端必须显式使用独立测试数据库和输出目录，禁止写入默认真实缓存")
    client = client or BaoStockClient(timeout_seconds=timeout_seconds, max_attempts=max_attempts)
    run = M1Run(operation, output_dir, client, now)
    run.result["requested_date"] = target_date.isoformat() if target_date else None
    run.result["database"] = str(database.resolve()) if operation == "collect" else None
    calendar_end = target_date or now.date()
    days = _calendar(run, calendar_end - timedelta(days=40), calendar_end)
    if days is None:
        if operation == "collect":
            store = MarketStore(database)
            store.export_csv(run.directory / "sample_bars.csv", list(SAMPLE_TYPES), calendar_end, calendar_end)
            run.result["database_row_count"] = store.row_count()
        return run.finish()
    target = _target(run, target_date, days)
    if target is None:
        return run.finish()
    instruments, probe_bars, passed = _probe(run, target)
    if operation == "doctor":
        # No market database writes by the diagnostic command.
        rows = [{"scope_notice": SCOPE, "mode": "research", **bar.model_dump(mode="json")} for bar in probe_bars]
        columns = list(rows[0]) if rows else ["scope_notice", "mode", "symbol", "trade_date", "provider", "adjustment_mode", "fetched_at"]
        with (run.directory / "sample_bars.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        run.result["status"] = "ok" if passed else ("partial" if probe_bars else "failed")
        return run.finish()

    store = MarketStore(database)
    history_start = start_date or target - timedelta(days=365)
    if history_start > target or (target - history_start).days > MAX_HISTORY_DAYS:
        run.fail("history_range", "采集区间需为正向且最多 366 天")
        return run.finish()
    run.result.update(start_date=history_start.isoformat(), end_date=target.isoformat(), refresh=refresh)
    if not passed:
        run.fail("history_gate", "单日小样本验证未全部成功，本轮不开展约一年历史采集")
        store.export_csv(run.directory / "sample_bars.csv", list(SAMPLE_TYPES), history_start, target)
        run.result["database_row_count"] = store.row_count()
        return run.finish()
    history_calendar = _calendar(run, history_start, target)
    if history_calendar is None:
        return run.finish()
    store.store_calendar(history_calendar)
    for instrument in instruments.values():
        store.store_instrument(instrument)
    trading = [item.calendar_date for item in history_calendar if item.is_trading_day]
    probe_items = run.result["items"]
    run.result["single_day_items"] = probe_items
    run.result["items"] = []
    stop_source = False
    for symbol, expected_type in SAMPLE_TYPES.items():
        item = {"symbol": symbol, "status": "ok", "rows": 0, "inserted": 0, "updated": 0, "unchanged": 0,
                "latest_date": None, "requested_windows": []}
        run.result["items"].append(item)
        if stop_source:
            item.update(status="not_attempted", reason="数据源访问受限，已停止后续采集")
            continue
        instrument = instruments[symbol]
        eligible = [day for day in trading if (instrument.ipo_date is None or day >= instrument.ipo_date)
                    and (instrument.out_date is None or day <= instrument.out_date)]
        complete_dates = {bar.trade_date for bar in store.read_bars([symbol], history_start, target) if not bar.quality_flags}
        windows = incremental_windows(eligible, complete_dates, refresh=refresh)
        # Always refresh the recent tail before spending the repair budget on old holes.
        if len(windows) > 1:
            windows = [windows[-1], *windows[:-1]]
        if len(windows) > MAX_WINDOWS_PER_SYMBOL:
            item.update(status="partial", reason="本次达到缺口请求窗口上限，重新运行可继续补齐")
        for begin, end in windows[:MAX_WINDOWS_PER_SYMBOL]:
            item["requested_windows"].append([begin.isoformat(), end.isoformat()])
            response = run.query("history", code=symbol, start_date=begin.isoformat(), end_date=end.isoformat(),
                                 security_type=expected_type, adjustment_mode="unadjusted")
            if not response["ok"]:
                item.update(status=response["status"], reason=response["error_msg"])
                run.fail("history", response["error_msg"], symbol=symbol, status=response["status"])
                stop_source = response["status"] in STOP_SOURCE_STATUSES
                break
            try:
                normalized = normalize_bars(response["rows"], instrument=instrument, start_date=begin, end_date=end,
                                            trading_dates=set(eligible), fetched_at=_fetched(response), sdk_version=response["sdk_version"])
                if not normalized:
                    raise ValueError("接口空响应；区间存在交易日，不能填零或当作已完整采集")
                changes = store.store_bars(normalized)
                item["rows"] += len(normalized)
                latest_returned = max(bar.trade_date for bar in normalized).isoformat()
                item["latest_date"] = max(item["latest_date"] or latest_returned, latest_returned)
                for key in ("inserted", "updated", "unchanged"):
                    item[key] += changes[key]
                expected_days = {day for day in eligible if begin <= day <= end}
                if {bar.trade_date for bar in normalized} != expected_days or any(bar.quality_flags for bar in normalized):
                    item.update(status="partial", reason="响应有缺失日期或字段，已保存有效记录，覆盖检查显示缺口")
            except ValueError as exc:
                item.update(status="validation_failed", reason=str(exc))
                run.fail("history_validation", exc, symbol=symbol)
                break
    coverage = store.coverage(list(SAMPLE_TYPES), history_start, target, target_date=target)
    run.result["coverage"] = coverage
    run.result["database_row_count"] = store.row_count()
    run.result["csv_record_count"] = store.export_csv(run.directory / "sample_bars.csv", list(SAMPLE_TYPES), history_start, target)
    run.result["status"] = "ok" if all(item["status"] == "ok" for item in run.result["items"]) and coverage.get("status") == "complete_within_scope" else "partial"
    run.result["latest_fetched_data_date"] = max((item["latest_date"] for item in run.result["items"] if item["latest_date"]), default=None)
    run.result["actual_latest_data_date"] = coverage["actual_latest_data_date"]
    return run.finish()


def check_market(*, start_date: date, target_date: date, database: Path = DEFAULT_DATABASE,
                 output_dir: Path = DEFAULT_EVIDENCE_DIR) -> dict:
    if start_date > target_date or (target_date - start_date).days > MAX_HISTORY_DAYS:
        raise ValueError("检查区间需为正向且最多 366 天")
    validate_paths(database, output_dir)
    if not database.is_file():
        raise ValueError("真实行情数据库尚不存在，请先运行 collect；不会用 DEMO 替补")
    store = MarketStore(database)
    coverage = store.coverage(list(SAMPLE_TYPES), start_date, target_date, target_date=target_date)
    directory = output_dir.resolve() / f"{datetime.now(SHANGHAI):%Y%m%dT%H%M%S%f}-check-{uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    result = {"operation": "check-data", "verification_kind": "local_database", "scope": SCOPE,
              "mode": "research", "network_access": "disabled", "database": str(database.resolve()),
              "start_date": start_date.isoformat(), "target_trade_date": target_date.isoformat(),
              "coverage": coverage, "database_row_count": store.row_count(),
              "csv_record_count": store.export_csv(directory / "sample_bars.csv", list(SAMPLE_TYPES), start_date, target_date),
              "run_directory": str(directory), "actual_generated_at": datetime.now(SHANGHAI).isoformat(),
              "status": "ok" if coverage.get("status") == "complete_within_scope" else "partial"}
    write_json(directory / "result.json", result)
    write_json(directory.parent / "latest_check-data.json", {"result_file": str(directory / "result.json"), "run_directory": str(directory), "status": result["status"]})
    return result
