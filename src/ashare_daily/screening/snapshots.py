"""Read the M1 database in one read-only transaction; freeze M2 input by hash."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3

from ashare_daily.market_schemas import CalendarDay, DailyBar, Instrument, SHANGHAI
from ashare_daily.m1_settings import SAMPLE_TYPES
from ashare_daily.screening.engine import canonical, digest
from ashare_daily.screening.settings import StrategyConfig
from ashare_daily.operations.paths import resolve_archived_path, resolve_input_path


def read_market_input(database: Path, target: date, config: StrategyConfig, *, sample_types: dict[str, str] | None = None) -> dict:
    path = Path(database).resolve()
    if not path.is_file():
        raise ValueError(f"真实数据库不存在：{path}；请先运行 M1 collect")
    if "demo" in {part.lower() for part in path.parts}:
        raise ValueError("M2 不接受 DEMO 数据库")
    if target > datetime.now(SHANGHAI).date():
        raise ValueError("分析交易日不能晚于当前北京时间日期")
    sample_types = dict(SAMPLE_TYPES if sample_types is None else sample_types)
    if config.benchmark_id not in sample_types or sample_types[config.benchmark_id] != "index":
        raise ValueError("基准必须明确来自现有配置的指数样本")
    issues: dict[str, list[str]] = {}
    calendar_issues = []
    floor = target - timedelta(days=365)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("BEGIN")
        metadata = dict(connection.execute("SELECT key,value FROM market_metadata"))
        if metadata.get("verification_kind") == "offline_test":
            raise ValueError("离线测试数据库不能用作真实行情输入")
        if metadata.get("mode") != "research" or metadata.get("schema_version") != "m1-baostock-market-v1":
            raise ValueError("数据库不是受支持的 M1 真实行情库")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite 完整性检查失败")
        calendar_rows = connection.execute("SELECT calendar_date,is_trading_day,payload_json FROM trading_calendar WHERE provider='baostock' AND calendar_date BETWEEN ? AND ? ORDER BY calendar_date", (floor.isoformat(), target.isoformat())).fetchall()
        calendar = {}
        calendar_payloads = []
        for day, flag, payload in calendar_rows:
            item = CalendarDay.model_validate_json(payload)
            if item.calendar_date.isoformat() != day or item.is_trading_day != bool(flag):
                raise ValueError("日历主键/状态与保存的来源契约不一致")
            calendar[day] = bool(flag)
            calendar_payloads.append(item.model_dump(mode="json"))
        trading_dates = sorted(day for day, trading in calendar.items() if trading)[-config.history_days:]
        start = date.fromisoformat(trading_dates[0]) if trading_dates else floor
        for offset in range((target - start).days + 1):
            day = (start + timedelta(days=offset)).isoformat()
            if day not in calendar:
                calendar_issues.append(f"交易日历缺失 {day}，不按星期推断")
        instruments, bars = [], []
        for symbol in sorted(sample_types):
            row = connection.execute("SELECT payload_json FROM instruments WHERE provider='baostock' AND symbol=?", (symbol,)).fetchone()
            try:
                if row is None:
                    raise ValueError("证券主数据缺失")
                instrument = Instrument.model_validate_json(row[0])
                if instrument.symbol != symbol:
                    raise ValueError("证券主键与保存的来源契约不一致")
                instruments.append(instrument.model_dump(mode="json"))
            except ValueError as exc:
                issues.setdefault(symbol, []).append(f"主数据校验失败：{exc}")
            rows = connection.execute("SELECT trade_date,adjustment_mode,payload_json FROM daily_bars WHERE provider='baostock' AND symbol=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date", (symbol, start.isoformat(), target.isoformat())).fetchall()
            for day, adjustment, payload in rows:
                try:
                    bar = DailyBar.model_validate_json(payload)
                    if bar.symbol != symbol or bar.trade_date.isoformat() != day or adjustment != bar.adjustment_mode.value:
                        raise ValueError("日线主键或口径与保存的来源契约不一致")
                    bars.append(bar.model_dump(mode="json"))
                except ValueError as exc:
                    issues.setdefault(symbol, []).append(f"{day} 日线校验失败：{exc}")
    return {
        "schema_version": "m2-input-v1", "verification_kind": "local_real_data",
        "trade_date": target.isoformat(), "strategy_config": config.model_dump(mode="json"),
        "database_path": str(path), "sample_types": sample_types,
        "trading_dates": trading_dates, "target_is_trading": calendar.get(target.isoformat()),
        "calendar_records": [item for item in calendar_payloads if item["calendar_date"] >= start.isoformat()],
        "calendar_issues": calendar_issues, "instruments": instruments, "raw_bars": bars,
        "source_issues": issues,
        "eligibility_states": {symbol: {"delisting_period": None, "effective_date": None, "evidence_id": None,
            "reason": "来源未提供退市整理期状态，不猜测"} for symbol, kind in sample_types.items() if kind == "stock"},
    }


def verify_bundle(bundle: dict) -> dict:
    from ashare_daily.m2_data import canonical_hash

    if bundle.get("schema_version") != "m2-adjusted-bundle-v1":
        raise ValueError("不支持的调整数据包版本")
    content = {key: value for key, value in bundle.items() if key != "manifest_hash"}
    if bundle.get("manifest_hash") != canonical_hash(content):
        raise ValueError("调整数据包哈希不匹配；拒绝使用已修改的数据")
    return bundle


def read_bundle(path: Path | str, *, anchor: Path | None = None) -> dict:
    resolved = resolve_input_path(path) if anchor is None else resolve_archived_path(path, anchor=anchor)
    return verify_bundle(json.loads(resolved.read_text(encoding="utf-8")))


def freeze_input(inputs: dict, bundle: dict | None, root: Path) -> tuple[dict, Path]:
    content = dict(inputs)
    content["source_issues"] = {key: list(value) for key, value in inputs.get("source_issues", {}).items()}
    if bundle is not None:
        verify_bundle(bundle)
        if bundle.get("trading_dates") != inputs["trading_dates"] or bundle.get("symbol_types") != inputs["sample_types"]:
            raise ValueError("调整数据包与冻结样本/交易日窗口不匹配")
        if bundle.get("verification_kind") == "offline_test" and inputs["verification_kind"] != "offline_test":
            raise ValueError("不能把测试数据包用于真实行情快照")
        raw = {(bar["symbol"], bar["trade_date"]): bar for bar in inputs["raw_bars"]}
        for symbol, series in bundle.get("series", {}).items():
            for bar in series.get("bars", []):
                original = raw.get((symbol, bar["trade_date"]))
                if original is None:
                    continue
                fields = ["volume_shares", "amount_cny"]
                if inputs["sample_types"].get(symbol) == "stock":
                    fields += ["tradestatus", "is_st"]
                from ashare_daily.factors.trend import number
                for field in fields:
                    old, new = original.get(field), bar.get(field)
                    if field in {"volume_shares", "amount_cny"}:
                        old, new = number(old), number(new)
                    if old != new:
                        content["source_issues"].setdefault(symbol, []).append(f"{bar['trade_date']} 未复权/调整响应 {field} 不一致，需重新核对来源版本")
    content["adjusted_data"] = bundle or {"series": {}, "status": "missing", "reason": "缺少调整价格数据包；可显式 --fetch-adjusted 获取必要小样本窗口"}
    content["frozen_at"] = datetime.now(SHANGHAI).isoformat()
    snapshot_id = "m2-" + digest(content)
    snapshot = {**content, "snapshot_id": snapshot_id}
    destination = Path(root).resolve() / "snapshots" / f"{snapshot_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
    return snapshot, destination


def load_snapshot(value: Path | str, root: Path) -> tuple[dict, Path]:
    path = Path(value)
    if re.fullmatch(r"m2-[0-9a-f]{64}", str(value)):
        path = Path(root) / "snapshots" / f"{value}.json"
    path = resolve_archived_path(path, anchor=root).resolve()
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("schema_version") != "m2-input-v1":
        raise ValueError("快照版本不受支持")
    content = {key: value for key, value in snapshot.items() if key != "snapshot_id"}
    if snapshot.get("snapshot_id") != "m2-" + digest(content):
        raise ValueError("快照哈希不匹配，不能重放被修改的依据")
    if snapshot["strategy_config"].get("workflow_version") == "m2.1":
        from ashare_daily.screening.m21 import M21Config
        M21Config.model_validate(snapshot["strategy_config"])
    else:
        StrategyConfig.model_validate(snapshot["strategy_config"])
    if snapshot.get("adjusted_data", {}).get("schema_version"):
        verify_bundle(snapshot["adjusted_data"])
    return snapshot, path
