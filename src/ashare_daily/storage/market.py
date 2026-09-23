"""真实数据缓存、不可变内容版本和基于实际交易日历的范围覆盖检查。"""

from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Iterable, Iterator

from ashare_daily.market_schemas import AdjustmentMode, CalendarDay, DailyBar, Instrument


SCHEMA_VERSION = "m1-baostock-market-v1"
SCOPE_NOTICE = "小样本验证；仅为技术测试，不代表推荐股票或全市场数据。"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class MarketStore:
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        if "demo" in {part.lower() for part in self.path.parts} or self.path.name.lower() == "demo.sqlite3":
            raise ValueError("真实市场数据库不能写入 DEMO 路径")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if any(name.startswith("demo_") for name in tables):
                raise ValueError("拒绝把真实行情写入 DEMO 数据库")
            if tables and "market_metadata" not in tables:
                raise ValueError("已有数据库不是受管理的 M1 真实行情数据库，拒绝修改")
            if "market_metadata" in tables:
                metadata = dict(connection.execute("SELECT key, value FROM market_metadata"))
                if metadata.get("mode") != "research" or metadata.get("schema_version") != SCHEMA_VERSION:
                    raise ValueError("数据库模式或 schema 版本不匹配，拒绝修改")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS market_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS instruments (
                    provider TEXT NOT NULL, symbol TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    raw_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY (provider, symbol)
                );
                CREATE TABLE IF NOT EXISTS instrument_versions (
                    provider TEXT NOT NULL, symbol TEXT NOT NULL, raw_hash TEXT NOT NULL,
                    version_first_seen_at TEXT NOT NULL, last_fetched_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (provider, symbol, raw_hash)
                );
                CREATE TABLE IF NOT EXISTS trading_calendar (
                    provider TEXT NOT NULL, calendar_date TEXT NOT NULL, is_trading_day INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL, raw_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (provider, calendar_date)
                );
                CREATE TABLE IF NOT EXISTS calendar_versions (
                    provider TEXT NOT NULL, calendar_date TEXT NOT NULL, raw_hash TEXT NOT NULL,
                    version_first_seen_at TEXT NOT NULL, last_fetched_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (provider, calendar_date, raw_hash)
                );
                CREATE TABLE IF NOT EXISTS daily_bars (
                    provider TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
                    adjustment_mode TEXT NOT NULL CHECK (adjustment_mode = 'unadjusted'),
                    first_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL, raw_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (provider, symbol, trade_date, adjustment_mode),
                    FOREIGN KEY (provider, symbol) REFERENCES instruments(provider, symbol)
                );
                CREATE TABLE IF NOT EXISTS daily_bar_versions (
                    provider TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
                    adjustment_mode TEXT NOT NULL CHECK (adjustment_mode = 'unadjusted'),
                    raw_hash TEXT NOT NULL, version_first_seen_at TEXT NOT NULL,
                    last_fetched_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY (provider, symbol, trade_date, adjustment_mode, raw_hash)
                );
            """)
            connection.executemany("INSERT OR IGNORE INTO market_metadata VALUES (?,?)", [
                ("mode", "research"), ("provider", "baostock"), ("schema_version", SCHEMA_VERSION),
                ("scope_notice", SCOPE_NOTICE), ("adjustment_policy", "unadjusted_only"),
            ])

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _payload(record, existing) -> dict:
        payload = record.model_dump(mode="json")
        if existing:
            if record.fetched_at < datetime.fromisoformat(existing["fetched_at"]):
                raise ValueError("抓取时间早于当前存储版本，不能覆盖较新结果")
            payload["first_seen_at"] = existing["first_seen_at"]
        return payload

    def store_instrument(self, instrument: Instrument) -> None:
        instrument = Instrument.model_validate(instrument.model_dump())
        with self._connection() as connection:
            existing = connection.execute("SELECT * FROM instruments WHERE provider=? AND symbol=?", (
                instrument.provider, instrument.symbol,
            )).fetchone()
            payload = self._payload(instrument, existing)
            connection.execute("""
                INSERT INTO instruments VALUES (?,?,?,?,?,?)
                ON CONFLICT(provider,symbol) DO UPDATE SET fetched_at=excluded.fetched_at,
                    raw_hash=excluded.raw_hash,payload_json=excluded.payload_json
            """, (instrument.provider, instrument.symbol, payload["first_seen_at"], payload["fetched_at"],
                  instrument.raw_hash, _json(payload)))
            connection.execute("""
                INSERT INTO instrument_versions VALUES (?,?,?,?,?,?)
                ON CONFLICT(provider,symbol,raw_hash) DO UPDATE SET last_fetched_at=excluded.last_fetched_at
            """, (instrument.provider, instrument.symbol, instrument.raw_hash, payload["fetched_at"],
                  payload["fetched_at"], _json(payload)))

    def store_calendar(self, days: Iterable[CalendarDay]) -> None:
        days = [CalendarDay.model_validate(item.model_dump()) for item in days]
        if len({item.calendar_date for item in days}) != len(days):
            raise ValueError("同一批次存在重复日历日期")
        with self._connection() as connection:
            for day in days:
                existing = connection.execute("SELECT * FROM trading_calendar WHERE provider=? AND calendar_date=?", (
                    day.provider, day.calendar_date.isoformat(),
                )).fetchone()
                payload = self._payload(day, existing)
                connection.execute("""
                    INSERT INTO trading_calendar VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(provider,calendar_date) DO UPDATE SET is_trading_day=excluded.is_trading_day,
                        fetched_at=excluded.fetched_at,raw_hash=excluded.raw_hash,payload_json=excluded.payload_json
                """, (day.provider, day.calendar_date.isoformat(), int(day.is_trading_day), payload["first_seen_at"],
                      payload["fetched_at"], day.raw_hash, _json(payload)))
                connection.execute("""
                    INSERT INTO calendar_versions VALUES (?,?,?,?,?,?)
                    ON CONFLICT(provider,calendar_date,raw_hash) DO UPDATE SET last_fetched_at=excluded.last_fetched_at
                """, (day.provider, day.calendar_date.isoformat(), day.raw_hash, payload["fetched_at"],
                      payload["fetched_at"], _json(payload)))

    def store_bars(self, bars: Iterable[DailyBar]) -> dict[str, int]:
        bars = [DailyBar.model_validate(item.model_dump()) for item in bars]
        result = {"inserted": 0, "updated": 0, "unchanged": 0}
        if len({bar.symbol for bar in bars}) > 1:
            raise ValueError("store_bars 为单证券原子事务；请逐证券保存")
        keys = {(bar.provider, bar.symbol, bar.trade_date, bar.adjustment_mode) for bar in bars}
        if len(keys) != len(bars):
            raise ValueError("同一批次存在重复日线记录")
        with self._connection() as connection:
            for bar in bars:
                instrument_row = connection.execute("SELECT payload_json FROM instruments WHERE provider=? AND symbol=?", (
                    bar.provider, bar.symbol,
                )).fetchone()
                if instrument_row is None:
                    raise ValueError(f"入库前必须校验并保存证券身份: {bar.symbol}")
                instrument = Instrument.model_validate_json(instrument_row[0])
                if instrument.security_type == "stock" and (bar.tradestatus is None or bar.is_st is None):
                    raise ValueError("股票日线不得缺少交易/ST 状态")
                if bar.price_unit != ("CNY" if instrument.security_type == "stock" else "index_points"):
                    raise ValueError("价格单位与证券类型不一致")
                calendar_row = connection.execute("SELECT is_trading_day FROM trading_calendar WHERE provider=? AND calendar_date=?", (
                    bar.provider, bar.trade_date.isoformat(),
                )).fetchone()
                if calendar_row is None or not calendar_row[0]:
                    raise ValueError(f"入库日线日期未经交易日历确认: {bar.trade_date}")
                key = (bar.provider, bar.symbol, bar.trade_date.isoformat(), bar.adjustment_mode.value)
                existing = connection.execute("""
                    SELECT * FROM daily_bars WHERE provider=? AND symbol=? AND trade_date=? AND adjustment_mode=?
                """, key).fetchone()
                payload = self._payload(bar, existing)
                action = "inserted" if existing is None else ("unchanged" if existing["raw_hash"] == bar.raw_hash else "updated")
                result[action] += 1
                connection.execute("""
                    INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(provider,symbol,trade_date,adjustment_mode) DO UPDATE SET
                        fetched_at=excluded.fetched_at,raw_hash=excluded.raw_hash,payload_json=excluded.payload_json
                """, (*key, payload["first_seen_at"], payload["fetched_at"], bar.raw_hash, _json(payload)))
                connection.execute("""
                    INSERT INTO daily_bar_versions VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(provider,symbol,trade_date,adjustment_mode,raw_hash)
                    DO UPDATE SET last_fetched_at=excluded.last_fetched_at
                """, (*key, bar.raw_hash, payload["fetched_at"], payload["fetched_at"], _json(payload)))
        return result

    def get_instrument(self, symbol: str) -> Instrument | None:
        with self._connection() as connection:
            row = connection.execute("SELECT payload_json FROM instruments WHERE provider='baostock' AND symbol=?", (symbol,)).fetchone()
        return Instrument.model_validate_json(row[0]) if row else None

    def calendar_dates(self, start_date: date, end_date: date) -> dict[date, bool]:
        with self._connection() as connection:
            rows = connection.execute("""
                SELECT calendar_date,is_trading_day FROM trading_calendar
                WHERE provider='baostock' AND calendar_date BETWEEN ? AND ? ORDER BY calendar_date
            """, (start_date.isoformat(), end_date.isoformat())).fetchall()
        return {date.fromisoformat(row[0]): bool(row[1]) for row in rows}

    def stored_dates(self, symbol: str, start_date: date, end_date: date) -> set[date]:
        with self._connection() as connection:
            rows = connection.execute("""
                SELECT trade_date FROM daily_bars WHERE provider='baostock' AND symbol=?
                AND trade_date BETWEEN ? AND ? AND adjustment_mode='unadjusted'
            """, (symbol, start_date.isoformat(), end_date.isoformat())).fetchall()
        return {date.fromisoformat(row[0]) for row in rows}

    def read_bars(self, symbols: Iterable[str], start_date: date, end_date: date) -> list[DailyBar]:
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            return []
        with self._connection() as connection:
            rows = connection.execute(f"""
                SELECT payload_json FROM daily_bars WHERE provider='baostock'
                AND symbol IN ({','.join('?' for _ in symbols)}) AND trade_date BETWEEN ? AND ?
                AND adjustment_mode='unadjusted' ORDER BY symbol,trade_date
            """, (*symbols, start_date.isoformat(), end_date.isoformat())).fetchall()
        return [DailyBar.model_validate_json(row[0]) for row in rows]

    def row_count(self) -> int:
        with self._connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]

    def coverage(
        self, symbols: Iterable[str], start_date: date, end_date: date, target_date: date | None = None,
    ) -> dict:
        if start_date > end_date:
            raise ValueError("覆盖检查开始日期不能晚于结束日期")
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            raise ValueError("覆盖检查必须明确样本范围")
        calendar = self.calendar_dates(start_date, end_date)
        expected_calendar = {start_date + timedelta(days=n) for n in range((end_date - start_date).days + 1)}
        absent_calendar = sorted(expected_calendar - set(calendar))
        trading_dates = {day for day, trading in calendar.items() if trading}
        bars = self.read_bars(symbols, start_date, end_date)
        by_symbol: dict[str, dict[date, DailyBar]] = {symbol: {} for symbol in symbols}
        for bar in bars:
            by_symbol[bar.symbol][bar.trade_date] = bar
        symbol_results = []
        expected_count = covered_count = unknown_count = explained_count = quality_count = 0
        actual_latest = max((bar.trade_date for bar in bars), default=None)
        for symbol in symbols:
            instrument = self.get_instrument(symbol)
            records = by_symbol[symbol]
            expected = set(trading_dates)
            explained = []
            for day in sorted(trading_dates):
                if instrument and instrument.ipo_date and day < instrument.ipo_date:
                    explained.append({"date": day.isoformat(), "reason": "before_ipo"})
                    expected.discard(day)
                elif instrument and instrument.out_date and day > instrument.out_date:
                    explained.append({"date": day.isoformat(), "reason": "after_delisting"})
                    expected.discard(day)
            missing = sorted(expected - set(records))
            bad = [{"date": day.isoformat(), "flags": bar.quality_flags} for day, bar in sorted(records.items()) if bar.quality_flags]
            suspended = [day.isoformat() for day, bar in sorted(records.items()) if bar.tradestatus is False]
            latest = max(records, default=None)
            # A calendar date missing altogether is unknown, never assumed suspended.
            status_unknown = [day.isoformat() for day, bar in sorted(records.items())
                              if instrument and instrument.security_type == "stock" and (bar.tradestatus is None or bar.is_st is None)]
            expected_count += len(expected)
            covered_count += len(expected & set(records))
            unknown_count += len(missing)
            quality_count += len(bad)
            explained_count += len(explained)
            symbol_results.append({
                "symbol": symbol, "instrument_verified": instrument is not None,
                "expected_count": len(expected), "covered_count": len(expected & set(records)),
                "missing_dates": [day.isoformat() for day in missing], "explained_dates": explained,
                "suspension_dates": suspended, "status_unknown_dates": status_unknown,
                "quality_issues": bad, "actual_latest_data_date": latest.isoformat() if latest else None,
                "target_date_present": target_date in records if target_date else None,
                "last_fetched_at": max((bar.fetched_at for bar in records.values()), default=None),
            })
        missing_symbols = [row["symbol"] for row in symbol_results if row["missing_dates"] or not row["instrument_verified"]]
        complete = not absent_calendar and not missing_symbols and not quality_count
        if target_date is not None:
            target_status = "calendar_unknown" if target_date not in calendar else (
                "non_trading_day" if not calendar[target_date] else
                "current" if all(row["target_date_present"] for row in symbol_results) else "stale_or_missing"
            )
        else:
            target_status = "not_requested"
        with self._connection() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            version_count = connection.execute("SELECT COUNT(*) FROM daily_bar_versions").fetchone()[0]
        for row in symbol_results:
            if row["last_fetched_at"]:
                row["last_fetched_at"] = row["last_fetched_at"].isoformat()
        return {
            "mode": "research", "scope_notice": SCOPE_NOTICE, "market_coverage_verified": False,
            "provider": "baostock", "adjustment_mode": AdjustmentMode.UNADJUSTED.value,
            "database_path": str(self.path), "symbols": symbols,
            "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
            "target_trade_date": target_date.isoformat() if target_date else None,
            "actual_latest_data_date": actual_latest.isoformat() if actual_latest else None,
            "target_data_status": target_status, "calendar_day_count": len(calendar),
            "missing_calendar_dates": [day.isoformat() for day in absent_calendar],
            "expected_trading_dates": [day.isoformat() for day in sorted(trading_dates)],
            "expected_count": expected_count, "covered_count": covered_count,
            "unknown_missing_count": unknown_count, "explained_count": explained_count,
            "quality_issue_count": quality_count, "missing_symbols": missing_symbols,
            "status": "complete_within_scope" if complete and integrity == "ok" else "partial",
            "sqlite_integrity": integrity, "stored_version_count": version_count,
            "symbol_results": symbol_results,
        }

    def export_csv(self, path: Path | str, symbols: Iterable[str], start_date: date, end_date: date) -> int:
        bars = self.read_bars(symbols, start_date, end_date)
        destination = Path(path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        fields = ["scope_notice", "mode", "provider", "symbol", "trade_date", "adjustment_mode", "open", "high", "low", "close",
                  "preclose", "price_unit", "volume_shares", "volume_unit", "amount_cny", "amount_unit", "tradestatus", "is_st",
                  "turnover_ratio", "pct_change_ratio", "fetched_at", "first_seen_at", "raw_hash", "content_version", "sdk_version", "parameters", "quality_flags"]
        with destination.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for bar in bars:
                payload = bar.model_dump(mode="json")
                payload.update(scope_notice=SCOPE_NOTICE, mode="research", quality_flags="|".join(bar.quality_flags))
                payload["parameters"] = _json(bar.parameters)
                writer.writerow(payload)
        return len(bars)
