"""F2 deterministic history planning, quality checks and additive version storage.

No network, model, screening or publishing is performed here. Adjusted prices are
stored only as a complete provider response window, never as appendable bars.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Iterable
from zoneinfo import ZoneInfo

from ashare_daily.providers.baostock import BaoStockClient, raw_hash, validate_request
from ashare_daily.providers.base import DailyBarRequest, SecurityIdentity
from ashare_daily.operations.backup import _io

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCOPES = {"sse_szse_a", "all_a"}
MODES = {"unadjusted": "3", "forward_adjusted": "2"}
QUALITY_RULES_VERSION = "f2-quality-v2"
STORAGE_PROVIDERS = frozenset({"baostock", "eastmoney", "sina"})


def _provider_name(provider: str) -> str:
    if not isinstance(provider, str) or provider not in STORAGE_PROVIDERS:
        raise ValueError("explicit supported market provider required")
    return provider


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _day(value: str | date) -> date:
    if isinstance(value, datetime):
        raise ValueError("use an explicit Asia/Shanghai calendar date")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError("date must use YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD")
    return parsed


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(SHANGHAI)


def plan_history(*, target_date: str | date, calendar: dict, calendar_verified: bool,
                 listing_date: str | date | None, lookback: int = 320,
                 stored_dates: Iterable[str | date] = (), recheck_days: int = 5) -> dict:
    """Use an explicitly verified natural-day calendar; never generate trading days."""
    target = _day(target_date)
    if type(lookback) is not int or not 1 <= lookback <= 320 or type(recheck_days) is not int or not 0 <= recheck_days <= lookback:
        raise ValueError("invalid history lookback or recheck length")
    days = {_day(key): value for key, value in calendar.items()}
    if any(type(value) is not bool for value in days.values()):
        raise ValueError("calendar statuses must be boolean evidence")
    if len(days) != len(calendar):
        raise ValueError("duplicate normalized calendar dates")
    blockers = []
    if calendar_verified is not True:
        blockers.append("calendar_unverified")
    if target not in days:
        blockers.append("target_calendar_missing")
    elif not days[target]:
        blockers.append("target_not_trading_day")
    available = sorted(day for day, opened in days.items() if opened and day <= target)
    listing = _day(listing_date) if listing_date else None
    if listing is None:
        blockers.append("listing_date_unknown")
    elif listing > target:
        blockers.append("not_yet_listed")
    eligible = [day for day in available if listing is None or day >= listing]
    expected = eligible[-lookback:]
    if not expected:
        blockers.append("history_window_empty")
    else:
        required_start = max(listing, min(days)) if listing else min(days)
        if len(expected) < lookback and (listing is None or listing < min(days)):
            blockers.append("calendar_window_insufficient")
        begin = expected[0] if len(expected) == lookback else required_start
        missing_calendar = [begin + timedelta(days=index) for index in range((target - begin).days + 1)
                            if begin + timedelta(days=index) not in days]
        if missing_calendar:
            blockers.append("calendar_natural_day_gap")
    stored = {_day(day) for day in stored_dates}
    missing = set(expected) - stored
    requested = missing | (set(expected[-recheck_days:]) if recheck_days else set())
    # Split at already-covered sessions and at the legacy 366-natural-day bound.
    ranges, current = [], []
    for day in expected:
        if day not in requested or (current and (day - current[0]).days > 365):
            if current:
                ranges.append({"start_date": current[0].isoformat(), "end_date": current[-1].isoformat()})
                current = []
        if day in requested:
            current.append(day)
    if current:
        ranges.append({"start_date": current[0].isoformat(), "end_date": current[-1].isoformat()})
    return {"target_date": target.isoformat(), "window_start": expected[0].isoformat() if expected else None,
            "window_end": target.isoformat(), "expected_dates": [day.isoformat() for day in expected],
            "missing_dates": [day.isoformat() for day in sorted(missing)], "raw_ranges": ranges,
            "lookback_target": lookback, "expected_count": len(expected), "blockers": blockers,
            "plan_verified": not blockers}


def _number(row, name, *, integer=False, signed=False):
    value = row.get(name, "")
    if value == "":
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid_number:" + name) from exc
    if not number.is_finite() or (not signed and number < 0) or (integer and number != number.to_integral_value()):
        raise ValueError("invalid_number:" + name)
    return number


def _flag(row, name):
    value = row.get(name, "")
    if value == "":
        return None
    if value not in {"0", "1"}:
        raise ValueError("invalid_status:" + name)
    return value == "1"


def _numeric_text(value):
    return None if value is None else format(value.normalize(), "f")


def normalize_baostock_rows(rows: Iterable[dict], *, security_id: str, symbol: str,
                            start_date: str | date, end_date: str | date,
                            trading_dates: Iterable[str | date], adjustment_mode: str = "unadjusted") -> dict:
    """Preserve valid facts and unknown statuses; quarantine invalid rows in audit."""
    start, end = _day(start_date), _day(end_date)
    if not security_id or not symbol or start > end or adjustment_mode not in MODES:
        raise ValueError("invalid security, range or adjustment mode")
    expected = {_day(day) for day in trading_dates if start <= _day(day) <= end}
    raw_rows = list(rows)
    records, issues, seen = [], [], {}
    for index, row in enumerate(raw_rows):
        if isinstance(row, dict) and isinstance(row.get("date"), str):
            seen.setdefault(row["date"], []).append(index)
    duplicates = {key for key, positions in seen.items() if len(positions) > 1}
    for index, row in enumerate(raw_rows):
        try:
            if not isinstance(row, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in row.items()):
                raise ValueError("raw_fields_must_be_strings")
            day = _day(row.get("date"))
            if row["date"] in duplicates:
                raise ValueError("duplicate_date")
            if row.get("code") != symbol:
                raise ValueError("symbol_mismatch")
            if not start <= day <= end or day not in expected:
                raise ValueError("date_not_in_verified_requested_calendar")
            if row.get("adjustflag") != MODES[adjustment_mode]:
                raise ValueError("adjustment_mode_mismatch")
            prices = {name: _number(row, name) for name in ("open", "high", "low", "close", "preclose")}
            if any(value is not None and value <= 0 for value in prices.values()):
                raise ValueError("price_must_be_positive")
            high, low = prices["high"], prices["low"]
            if high is not None and low is not None and high < low:
                raise ValueError("ohlc_range_invalid")
            if any(value is not None and ((high is not None and value > high) or (low is not None and value < low))
                   for value in (prices["open"], prices["close"])):
                raise ValueError("ohlc_range_invalid")
            volume, amount = _number(row, "volume", integer=True), _number(row, "amount")
            trading, st = _flag(row, "tradestatus"), _flag(row, "isST")
            if trading is False and (volume not in (None, 0) or amount not in (None, 0)):
                raise ValueError("suspension_volume_amount_conflict")
            # Preserve the existing M1 NULL flags even for an evidenced suspension.
            # Suspension changes the quote denominator, not the stored field facts.
            flags = ["missing_" + name for name, value in prices.items() if value is None]
            if volume is None:
                flags.append("missing_volume_shares")
            if amount is None:
                flags.append("missing_amount_cny")
            if (volume == 0 and amount is not None and amount > 0) or (amount == 0 and volume is not None and volume > 0):
                flags.append("volume_amount_inconsistent")
            turnover = _number(row, "turn")
            provider_change = _number(row, "pctChg", signed=True)
            reference_change = ((prices["close"] - prices["preclose"]) / prices["preclose"]
                                if prices["close"] is not None and prices["preclose"] is not None else None)
            record = {"security_id": security_id, "provider": "baostock", "symbol": symbol,
                      "trade_date": day.isoformat(), "adjustment_mode": adjustment_mode,
                      **{name: _numeric_text(value) for name, value in prices.items()},
                      "volume_shares": int(volume) if volume is not None else None,
                      "amount_cny": _numeric_text(amount), "tradestatus": trading, "is_st": st,
                      "turnover_ratio": _numeric_text(turnover / 100) if turnover is not None else None,
                      "provider_change_ratio": _numeric_text(provider_change / 100) if provider_change is not None else None,
                      "reference_change_ratio": _numeric_text(reference_change),
                      "price_unit": "CNY", "volume_unit": "shares", "amount_unit": "CNY",
                      "source_units": {"price": "CNY", "volume": "shares", "amount": "CNY", "turn": "percent", "pctChg": "percent"},
                      "after_hours_volume_inclusion": "unverified_no_addition", "quality_flags": flags}
            records.append(record)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append({"row_index": index, "date": row.get("date") if isinstance(row, dict) else None, "reason": str(exc)})
    records.sort(key=lambda item: item["trade_date"])
    observed = {record["trade_date"] for record in records}
    missing = sorted(day.isoformat() for day in expected if day.isoformat() not in observed)
    suspended = [record["trade_date"] for record in records if record["tradestatus"] is False]
    status_unknown = [record["trade_date"] for record in records if record["tradestatus"] is None or record["is_st"] is None]
    trading_unknown = [record["trade_date"] for record in records if record["tradestatus"] is None]
    bad_dates = [record["trade_date"] for record in records if record["quality_flags"]]
    bad_quote_dates = [record["trade_date"] for record in records if record["quality_flags"] and record["tradestatus"] is not False]
    quotes = [record["trade_date"] for record in records if record["tradestatus"] is True and not record["quality_flags"]]
    reviews = [{"date": after["trade_date"], "reason": "reference_preclose_differs_from_previous_close"}
               for before, after in zip(records, records[1:]) if before["close"] is not None and after["preclose"] is not None
               and Decimal(before["close"]) != Decimal(after["preclose"])]
    quote_complete = bool(expected) and not missing and not issues and not bad_quote_dates and not trading_unknown
    return {"quality_rules_version": QUALITY_RULES_VERSION,
            "security_id": security_id, "symbol": symbol, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "adjustment_mode": adjustment_mode, "expected_dates": [day.isoformat() for day in sorted(expected)],
            "records": records, "raw_row_count": len(raw_rows), "missing_dates": missing,
            "quality_issues": issues, "quality_issue_dates": bad_dates, "quote_quality_issue_dates": bad_quote_dates, "review_flags": reviews,
            "suspended_dates": suspended, "status_unknown_dates": status_unknown, "trading_status_unknown_dates": trading_unknown,
            "valid_quote_dates": quotes, "quote_complete": quote_complete,
            "status_complete": bool(expected) and not missing and not issues and not status_unknown,
            "complete": quote_complete and not status_unknown and not bad_dates}


class F2MarketStore:
    """SQLite online-backup-compatible f2_* tables; M1 objects remain untouched."""

    def __init__(self, path: str | Path, *, mode: str = "research"):
        self.path, self.mode = Path(path).resolve(), mode
        parts = {part.casefold() for part in self.path.parts}
        if mode not in {"research", "offline_test"} or "demo" in parts or self.path.name.casefold() == "demo.sqlite3":
            raise ValueError("invalid F2 database mode/path")
        if mode == "offline_test" and "research" in parts:
            raise ValueError("offline_test must not write research paths")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if any(name.casefold().startswith("demo_") for name in tables):
                raise ValueError("DEMO database is not accepted")
            if tables and not tables.intersection({"market_metadata", "f2_schema"}):
                raise ValueError("existing database is not a managed market database")
            if "market_metadata" in tables:
                metadata = dict(connection.execute("SELECT key,value FROM market_metadata"))
                provenance = metadata.get("verification_kind")
                if metadata.get("mode") != "research" or metadata.get("schema_version") != "m1-baostock-market-v1" or (mode == "research" and provenance != "live_network") or (mode == "offline_test" and provenance != "offline_test"):
                    raise ValueError("legacy market provenance mode mismatch")
            existing = "f2_schema" in tables
            if existing:
                schema = connection.execute("SELECT version,mode FROM f2_schema").fetchall()
                if len(schema) != 1 or tuple(schema[0]) != (1, mode):
                    raise ValueError("F2 schema/provenance mode mismatch")
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS f2_schema(version INTEGER NOT NULL,mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS f2_bar_versions(
                    security_id TEXT NOT NULL,provider TEXT NOT NULL,trade_date TEXT NOT NULL,
                    fact_hash TEXT NOT NULL,first_seen_at TEXT NOT NULL,payload_json TEXT NOT NULL,
                    PRIMARY KEY(security_id,provider,trade_date,fact_hash));
                CREATE TABLE IF NOT EXISTS f2_bar_current(
                    security_id TEXT NOT NULL,provider TEXT NOT NULL,trade_date TEXT NOT NULL,
                    fact_hash TEXT NOT NULL,last_fetched_at TEXT NOT NULL,
                    PRIMARY KEY(security_id,provider,trade_date),
                    FOREIGN KEY(security_id,provider,trade_date,fact_hash)
                    REFERENCES f2_bar_versions(security_id,provider,trade_date,fact_hash));
                CREATE TABLE IF NOT EXISTS f2_batches(
                    batch_id TEXT PRIMARY KEY,security_id TEXT NOT NULL,scope TEXT NOT NULL,
                    adjustment_mode TEXT NOT NULL,fetched_at TEXT NOT NULL,
                    source_response_path TEXT NOT NULL,source_file_hash TEXT NOT NULL,
                    source_raw_hash TEXT NOT NULL,quality_json TEXT NOT NULL,provenance_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS f2_bar_observations(
                    batch_id TEXT NOT NULL REFERENCES f2_batches(batch_id),trade_date TEXT NOT NULL,
                    fact_hash TEXT NOT NULL,PRIMARY KEY(batch_id,trade_date));
                CREATE TABLE IF NOT EXISTS f2_adjustment_windows(
                    window_id TEXT PRIMARY KEY,security_id TEXT NOT NULL,provider TEXT NOT NULL,
                    window_start TEXT NOT NULL,window_end TEXT NOT NULL,anchor_kind TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,content_hash TEXT NOT NULL,payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS f2_window_observations(
                    window_id TEXT NOT NULL REFERENCES f2_adjustment_windows(window_id),
                    batch_id TEXT NOT NULL REFERENCES f2_batches(batch_id),PRIMARY KEY(window_id,batch_id));
                CREATE INDEX IF NOT EXISTS f2_version_dates ON f2_bar_versions(security_id,trade_date);
                CREATE INDEX IF NOT EXISTS f2_window_dates ON f2_adjustment_windows(security_id,window_end,first_seen_at);
                CREATE TRIGGER IF NOT EXISTS f2_bars_no_update BEFORE UPDATE ON f2_bar_versions
                    BEGIN SELECT RAISE(ABORT,'F2 facts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS f2_bars_no_delete BEFORE DELETE ON f2_bar_versions
                    BEGIN SELECT RAISE(ABORT,'F2 facts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS f2_windows_no_update BEFORE UPDATE ON f2_adjustment_windows
                    BEGIN SELECT RAISE(ABORT,'F2 windows are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS f2_windows_no_delete BEFORE DELETE ON f2_adjustment_windows
                    BEGIN SELECT RAISE(ABORT,'F2 windows are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS f2_batches_no_update BEFORE UPDATE ON f2_batches
                    BEGIN SELECT RAISE(ABORT,'F2 batches are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS f2_batches_no_delete BEFORE DELETE ON f2_batches
                    BEGIN SELECT RAISE(ABORT,'F2 batches are immutable'); END;
            """)
            if not existing:
                connection.execute("INSERT INTO f2_schema VALUES(1,?)", (mode,))

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save_batch(self, *, security_id: str, symbol: str, scope: str,
                   universe_snapshot_id: str, response: dict, source_response_path: str | Path,
                   source_response_hash: str, trading_dates: Iterable[str | date],
                   provenance_mode: str, adjustment_mode: str = "unadjusted", batch_id: str | None = None,
                   provider: str | None = None, request: DailyBarRequest | None = None) -> dict:
        """Verify archived response bytes and preserve every rejected/missing row audit."""
        expected_mode = "online" if self.mode == "research" else "offline_test"
        if provenance_mode != expected_mode or scope not in SCOPES or not universe_snapshot_id:
            raise ValueError("universe scope or response provenance is unverified")
        source = Path(source_response_path).resolve()
        if self.mode == "offline_test" and "research" in {part.casefold() for part in source.parts}:
            raise ValueError("offline_test source cannot be a research path")
        body = _io(source).read_bytes()
        if hashlib.sha256(body).hexdigest() != source_response_hash or json.loads(body.decode("utf-8-sig")) != response:
            raise ValueError("archived response file/hash mismatch")
        if not isinstance(response, dict):
            raise ValueError("source response schema must be an object")
        if response.get("provenance_mode", expected_mode) != expected_mode or response.get("verification_kind", expected_mode) not in {expected_mode, "live_network" if self.mode == "research" else "offline_test"}:
            raise ValueError("response contains incompatible provenance")
        actual_provider = _provider_name(response.get("provider", "baostock"))
        if provider is not None and _provider_name(provider) != actual_provider:
            raise ValueError("response provider differs from requested provider")
        trading_dates = list(trading_dates)
        parameters = response.get("parameters", {})
        if request is not None and (not isinstance(request, DailyBarRequest) or
                (request.identity.security_id, request.identity.symbol, request.identity.scope) != (security_id, symbol, scope) or
                request.adjustment_mode != adjustment_mode or request.parameters() != parameters or
                request.expected_dates != tuple(sorted(_day(day).isoformat() for day in trading_dates))):
            raise ValueError("source response differs from frozen request identity, dates or scope")
        if actual_provider == "baostock":
            # Keep the existing SDK protocol, numerical facts and archive IDs intact.
            if response.get("operation") not in {"history", "history_f2"} or response.get("ok") is not True or response.get("error_code") != "0":
                raise ValueError("source response did not succeed")
            validate_request(response["operation"], parameters)
            if parameters.get("code") != symbol or parameters.get("security_type") != "stock" or parameters.get("adjustment_mode", "unadjusted") != adjustment_mode:
                raise ValueError("response request identity or adjustment mismatch")
            fields, rows = response.get("fields"), response.get("rows")
            if not isinstance(fields, list) or len(fields) != len(set(fields)) or not isinstance(rows, list) or len(rows) > 500:
                raise ValueError("response schema or provider row bound invalid")
            if any(not isinstance(row, dict) or set(row) != set(fields) for row in rows):
                raise ValueError("response row fields differ from source schema")
            if response.get("raw_hash") != raw_hash(fields, rows):
                raise ValueError("source raw_hash mismatch")
            if not BaoStockClient._valid_worker_result(response, response["operation"], parameters):
                raise ValueError("source protocol, fields or login evidence is invalid")
        else:
            # This adapter validates its own HTTP bytes, schema, units and identity;
            # it is never passed through the unrelated BaoStock login/row protocol.
            if actual_provider == "sina":
                from ashare_daily.providers.sina_history import normalize_sina_response as normalize_source
            else:
                from ashare_daily.providers.eastmoney import normalize_eastmoney_response as normalize_source

            try:
                identity = SecurityIdentity(**response["identity"])
                archived_request = DailyBarRequest(identity, parameters["start_date"], parameters["end_date"],
                    tuple(sorted(_day(day).isoformat() for day in trading_dates)), adjustment_mode)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("HTTP source response request identity is invalid") from exc
            if (identity.security_id, identity.symbol, identity.scope) != (security_id, symbol, scope):
                raise ValueError("HTTP source response request identity or scope mismatch")
            if request is not None and request != archived_request:
                raise ValueError("HTTP source archived request differs from frozen request")
            request = archived_request
            quality = normalize_source(response, request, mode=self.mode)
        fetched = _time(response["fetched_at"])
        if fetched > datetime.now(SHANGHAI):
            raise ValueError("source fetched_at is in the future")
        start, end = _day(parameters["start_date"]), _day(parameters["end_date"])
        if end > fetched.date():
            raise ValueError("source requested future market date")
        if adjustment_mode == "forward_adjusted" and any(not start <= _day(day) <= end for day in trading_dates):
            raise ValueError("adjusted response is only a segment of the required complete window")
        if actual_provider == "baostock":
            quality = normalize_baostock_rows(rows, security_id=security_id, symbol=symbol, start_date=start,
                end_date=end, trading_dates=trading_dates, adjustment_mode=adjustment_mode)
            if adjustment_mode == "forward_adjusted" and response.get("operation") != "history_f2":
                raise ValueError("adjusted windows require a single complete history_f2 response")
        if adjustment_mode not in MODES:
            raise ValueError("unsupported storage adjustment mode")
        if any(record.get("provider") != actual_provider or record.get("security_id") != security_id or
               record.get("symbol") != symbol or record.get("adjustment_mode") != adjustment_mode
               for record in quality["records"]):
            raise ValueError("normalized source identity or adjustment mismatch")
        quality_version = QUALITY_RULES_VERSION if actual_provider == "baostock" else quality["quality_rules_version"]
        if not isinstance(quality_version, str) or not quality_version:
            raise ValueError("source quality rules version is required")
        provenance = {"provider": actual_provider, "mode": self.mode, "provenance_mode": provenance_mode,
                      "quality_rules_version": quality_version,
                      "scope": scope, "universe_snapshot_id": universe_snapshot_id, "parameters": parameters,
                      "sdk_version": response.get("sdk_version"), "fetched_at": fetched.isoformat(),
                      "anchor_kind": "provider_current_at_fetch" if adjustment_mode == "forward_adjusted" else None,
                      "point_in_time_adjustment_verified": False}
        if actual_provider in {"eastmoney", "sina"}:
            provenance.update(source_endpoint=response.get("source_endpoint"), source_symbol=response.get("source_symbol"),
                source_business_date=response.get("source_business_date"), source_schema_version=response.get("schema_version"),
                identity=response["identity"])
        generated_id = "f2-batch-" + _hash({"security_id": security_id, "file_hash": source_response_hash,
                                           "scope": scope, "universe_snapshot_id": universe_snapshot_id,
                                           "quality_rules_version": quality_version})[:32]
        batch_id = batch_id or generated_id
        result = {"inserted": 0, "updated": 0, "unchanged": 0, "batch_id": batch_id, "window_id": None,
                  "quality": quality, "source_file_hash": source_response_hash, "source_raw_hash": response["raw_hash"]}
        with self._connection() as connection:
            old_batch = connection.execute("SELECT * FROM f2_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if old_batch and (old_batch["source_file_hash"] != source_response_hash or old_batch["security_id"] != security_id or old_batch["quality_json"] != _json(quality) or old_batch["provenance_json"] != _json(provenance)):
                raise ValueError("batch_id conflicts with different frozen inputs")
            connection.execute("INSERT OR IGNORE INTO f2_batches VALUES(?,?,?,?,?,?,?,?,?,?)",
                (batch_id, security_id, scope, adjustment_mode, fetched.isoformat(), str(source), source_response_hash,
                 response["raw_hash"], _json(quality), _json(provenance)))
            if adjustment_mode == "unadjusted":
                for record in quality["records"]:
                    fact_hash = _hash(record)
                    key = (security_id, actual_provider, record["trade_date"])
                    current = connection.execute("SELECT fact_hash,last_fetched_at FROM f2_bar_current WHERE security_id=? AND provider=? AND trade_date=?", key).fetchone()
                    if current and fetched < _time(current["last_fetched_at"]):
                        raise ValueError("older observation cannot replace current market data")
                    if current and fetched == _time(current["last_fetched_at"]) and current["fact_hash"] != fact_hash:
                        raise ValueError("same-time source facts conflict")
                    action = "inserted" if current is None else "unchanged" if current["fact_hash"] == fact_hash else "updated"
                    result[action] += 1
                    connection.execute("INSERT OR IGNORE INTO f2_bar_versions VALUES(?,?,?,?,?,?)", (*key, fact_hash, fetched.isoformat(), _json(record)))
                    connection.execute("INSERT INTO f2_bar_current VALUES(?,?,?,?,?) ON CONFLICT(security_id,provider,trade_date) DO UPDATE SET fact_hash=excluded.fact_hash,last_fetched_at=excluded.last_fetched_at", (*key, fact_hash, fetched.isoformat()))
                    connection.execute("INSERT OR IGNORE INTO f2_bar_observations VALUES(?,?,?)", (batch_id, record["trade_date"], fact_hash))
            elif quality["quote_complete"] or (actual_provider == "sina" and quality.get("adjustment_window_complete") is True):
                window = {"security_id": security_id, "provider": actual_provider, "symbol": symbol,
                          "adjustment_mode": adjustment_mode, "window_start": start.isoformat(), "window_end": end.isoformat(),
                          "anchor_kind": "provider_current_at_fetch", "expected_dates": quality["expected_dates"],
                          "records": quality["records"], "point_in_time_adjustment_verified": False}
                if actual_provider == "sina":
                    # A complete observed price/factor window is a numerical
                    # artifact. It grants no quote/status/research completeness.
                    window.update(adjustment_anchor_hash=quality["adjustment_anchor_hash"],
                        raw_component_hash=quality["raw_component_hash"], factor_component_hash=quality["factor_component_hash"],
                        raw_fact_hashes=quality["raw_fact_hashes"],
                        quote_complete=False, status_complete=False, research_ready=False,
                        numeric_price_complete=quality["numeric_price_complete"],
                        numeric_history_complete=quality["numeric_history_complete"])
                content_hash = _hash(window)
                window_id = "f2-window-" + content_hash
                existing = connection.execute("SELECT window_id FROM f2_adjustment_windows WHERE window_id=?", (window_id,)).fetchone()
                connection.execute("INSERT OR IGNORE INTO f2_adjustment_windows VALUES(?,?,?,?,?,?,?,?,?)",
                    (window_id, security_id, actual_provider, start.isoformat(), end.isoformat(), "provider_current_at_fetch", fetched.isoformat(), content_hash, _json(window)))
                connection.execute("INSERT OR IGNORE INTO f2_window_observations VALUES(?,?)", (window_id, batch_id))
                result.update(window_id=window_id, unchanged=len(quality["records"]) if existing else 0,
                              inserted=0 if existing else len(quality["records"]))
        return result

    def read_bars(self, security_id: str, start_date: str | date, end_date: str | date, *,
                  provider: str = "baostock") -> list[dict]:
        provider = _provider_name(provider)
        with self._connection() as connection:
            rows = connection.execute("SELECT v.* FROM f2_bar_current c JOIN f2_bar_versions v USING(security_id,provider,trade_date,fact_hash) WHERE c.security_id=? AND c.provider=? AND c.trade_date BETWEEN ? AND ? ORDER BY c.trade_date",
                                      (security_id, provider, _day(start_date).isoformat(), _day(end_date).isoformat())).fetchall()
        result = []
        for row in rows:
            record = json.loads(row["payload_json"])
            if _hash(record) != row["fact_hash"]:
                raise ValueError("frozen raw fact hash mismatch")
            if record.get("provider") != provider or record.get("security_id") != security_id or record.get("trade_date") != row["trade_date"]:
                raise ValueError("frozen raw fact source identity mismatch")
            result.append({**record, "fact_hash": row["fact_hash"], "first_seen_at": row["first_seen_at"]})
        return result

    def stored_dates(self, security_id: str, start_date: str | date, end_date: str | date, *,
                     provider: str = "baostock") -> set[str]:
        # A malformed/missing-field row is retained as evidence but still needs repair.
        return {row["trade_date"] for row in self.read_bars(security_id, start_date, end_date, provider=provider)
                if (row["tradestatus"] is True and not row["quality_flags"]) or row["tradestatus"] is False}

    def get_bar_version(self, security_id: str, trade_date: str | date, fact_hash: str, *,
                        provider: str = "baostock") -> dict | None:
        """Read the exact frozen raw fact, independent of later source corrections."""
        provider = _provider_name(provider)
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM f2_bar_versions WHERE security_id=? AND provider=? AND trade_date=? AND fact_hash=?",
                (security_id, provider, _day(trade_date).isoformat(), fact_hash)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        if _hash(payload) != fact_hash:
            raise ValueError("frozen raw fact hash mismatch")
        if payload.get("provider") != provider or payload.get("security_id") != security_id or payload.get("trade_date") != row["trade_date"]:
            raise ValueError("frozen raw fact source identity mismatch")
        return {**payload, "fact_hash": fact_hash, "first_seen_at": row["first_seen_at"]}

    def get_adjustment_window(self, window_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM f2_adjustment_windows WHERE window_id=?", (window_id,)).fetchone()
            observations = connection.execute("SELECT b.* FROM f2_window_observations o JOIN f2_batches b USING(batch_id) WHERE o.window_id=? ORDER BY b.fetched_at", (window_id,)).fetchall()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        if _hash(payload) != row["content_hash"] or window_id != "f2-window-" + row["content_hash"]:
            raise ValueError("frozen adjustment window hash mismatch")
        if (payload.get("provider"), payload.get("security_id")) != (row["provider"], row["security_id"]) or any(
                record.get("provider") != row["provider"] or record.get("security_id") != row["security_id"]
                for record in payload.get("records", [])):
            raise ValueError("frozen adjustment window mixes source identities")
        return {**payload, "window_id": window_id, "content_hash": row["content_hash"], "first_seen_at": row["first_seen_at"],
                "observations": [{"batch_id": item["batch_id"], "fetched_at": item["fetched_at"],
                    "source_response_path": item["source_response_path"], "source_file_hash": item["source_file_hash"],
                    "source_raw_hash": item["source_raw_hash"]} for item in observations]}
