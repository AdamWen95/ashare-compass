"""BaoStock 原始字符串边界：身份、日期、单位与缺失值校验。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Iterable

from pydantic import ValidationError

from ashare_daily.market_schemas import CalendarDay, DailyBar, Instrument


class DataQualityError(ValueError):
    """响应成功但其内容不能满足所请求范围的数据契约。"""


def _date(value: Any, field: str, *, nullable: bool = False) -> date | None:
    if value is None or value == "":
        if nullable:
            return None
        raise DataQualityError(f"缺少 {field}")
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DataQualityError(f"{field} 日期格式不是 YYYY-MM-DD: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DataQualityError(f"{field} 日期无效: {value!r}") from exc


def _decimal(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DataQualityError(f"{field} 不能是布尔值")
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise DataQualityError(f"{field} 非法数值: {value!r}") from exc
    if not result.is_finite():
        raise DataQualityError(f"{field} 不是有限数值")
    return result


def _flag(value: Any, field: str, *, nullable: bool = False) -> bool | None:
    if nullable and (value is None or value == ""):
        return None
    if value not in ("0", "1"):
        raise DataQualityError(f"{field} 应为供应商字符串 0/1，实际 {value!r}")
    return value == "1"


def _provenance(row: dict[str, Any], fetched_at: datetime, sdk_version: str) -> dict[str, Any]:
    raw_hash = hashlib.sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return {
        "fetched_at": fetched_at, "first_seen_at": fetched_at, "raw_hash": raw_hash,
        "content_version": f"sha256:{raw_hash}", "sdk_version": sdk_version,
    }


def normalize_instrument(
    row: dict[str, str], *, expected_symbol: str, expected_type: str,
    fetched_at: datetime, sdk_version: str,
) -> Instrument:
    if row.get("code") != expected_symbol:
        raise DataQualityError(f"基础信息代码不匹配: 请求 {expected_symbol}，响应 {row.get('code')}")
    if expected_type not in ("stock", "index"):
        raise DataQualityError("本轮仅支持 stock/index")
    if row.get("type") != ("1" if expected_type == "stock" else "2"):
        raise DataQualityError(f"证券类型不匹配: {expected_symbol} type={row.get('type')}")
    if expected_type == "stock" and not re.fullmatch(
        r"(?:sh\.(?:600|601|603|605)\d{3}|sz\.(?:000|001|002|003)\d{3})", expected_symbol,
    ):
        raise DataQualityError("不在沪深主板普通 A 股代码范围；不得作为 A 股全集推断")
    if not row.get("code_name", "").strip():
        raise DataQualityError("证券名称缺失")
    if expected_type == "stock" and not row.get("ipoDate"):
        raise DataQualityError("股票上市日期缺失，不能确定历史应覆盖范围")
    try:
        return Instrument(
            symbol=expected_symbol, name=row["code_name"], security_type=expected_type,
            exchange=expected_symbol[:2].upper(), board="mainboard" if expected_type == "stock" else "index",
            ipo_date=_date(row.get("ipoDate"), "ipoDate", nullable=True),
            out_date=_date(row.get("outDate"), "outDate", nullable=True),
            status="listed" if _flag(row.get("status"), "status") else "delisted",
            parameters={"code": expected_symbol},
            **_provenance(row, fetched_at, sdk_version),
        )
    except ValidationError as exc:
        raise DataQualityError(str(exc)) from exc


def normalize_calendar(
    rows: Iterable[dict[str, str]], *, start_date: date, end_date: date,
    fetched_at: datetime, sdk_version: str,
) -> list[CalendarDay]:
    if start_date > end_date:
        raise DataQualityError("日历开始日期不能晚于结束日期")
    result: list[CalendarDay] = []
    seen: set[date] = set()
    for row in rows:
        day = _date(row.get("calendar_date"), "calendar_date")
        if not start_date <= day <= end_date:
            raise DataQualityError(f"日历日期超出请求范围: {day}")
        if day in seen:
            raise DataQualityError(f"交易日历重复日期: {day}")
        seen.add(day)
        try:
            result.append(CalendarDay(
                calendar_date=day, is_trading_day=_flag(row.get("is_trading_day"), "is_trading_day"),
                parameters={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
                **_provenance(row, fetched_at, sdk_version),
            ))
        except ValidationError as exc:
            raise DataQualityError(str(exc)) from exc
    expected = {start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)}
    if expected != seen:
        raise DataQualityError(f"交易日历缺少 {(len(expected - seen))} 天，不按工作日猜测")
    return sorted(result, key=lambda item: item.calendar_date)


def normalize_bars(
    rows: Iterable[dict[str, str]], *, instrument: Instrument, start_date: date, end_date: date,
    trading_dates: set[date], fetched_at: datetime, sdk_version: str,
) -> list[DailyBar]:
    if start_date > end_date:
        raise DataQualityError("日线开始日期不能晚于结束日期")
    result: list[DailyBar] = []
    seen: set[date] = set()
    for row in rows:
        if row.get("code") != instrument.symbol:
            raise DataQualityError(f"日线代码不匹配: {row.get('code')} != {instrument.symbol}")
        day = _date(row.get("date"), "date")
        if not start_date <= day <= end_date:
            raise DataQualityError(f"日线日期不在请求范围: {day}")
        if day not in trading_dates:
            raise DataQualityError(f"日线日期不在交易日历: {day}")
        if instrument.ipo_date and day < instrument.ipo_date:
            raise DataQualityError(f"日线早于上市日: {day}")
        if instrument.out_date and day > instrument.out_date:
            raise DataQualityError(f"日线晚于退市日: {day}")
        if day in seen:
            raise DataQualityError(f"响应内日线日期重复: {instrument.symbol} {day}")
        seen.add(day)
        is_stock = instrument.security_type == "stock"
        # 官方指数响应不提供 adjustflag；调用方固定以未复权请求，不能伪造该返回字段。
        # 股票必须实际返回 3，指数若意外返回明确的其他口径也拒绝。
        adjustment_flag = row.get("adjustflag")
        if (is_stock and adjustment_flag != "3") or (not is_stock and adjustment_flag not in (None, "", "3")):
            raise DataQualityError("M1 仅接受 BaoStock adjustflag=3 未复权，拒绝混入前/后复权")
        tradestatus = _flag(row.get("tradestatus"), "tradestatus", nullable=not is_stock)
        is_st = _flag(row.get("isST"), "isST", nullable=not is_stock)
        if not is_stock and (tradestatus is not None or is_st is not None):
            raise DataQualityError("指数没有股票交易/ST 状态字段，不得合成这些状态")
        prices = {name: _decimal(row.get(name), name) for name in ("open", "high", "low", "close", "preclose")}
        volume = _decimal(row.get("volume"), "volume")
        if volume is not None and (volume < 0 or volume != volume.to_integral_value()):
            raise DataQualityError("成交量必须是非负整数股")
        amount = _decimal(row.get("amount"), "amount")
        turnover = _decimal(row.get("turn"), "turn")
        pct_change = _decimal(row.get("pctChg"), "pctChg")
        flags = [f"missing_{name}" for name, value in prices.items() if value is None]
        if volume is None:
            flags.append("missing_volume_shares")
        if amount is None:
            flags.append("missing_amount_cny")
        # 缺失保留 NULL 并明确质量标记，覆盖检查会据此拒绝声称完整。
        if (volume == 0 and amount is not None and amount > 0) or (amount == 0 and volume is not None and volume > 0):
            flags.append("volume_amount_inconsistent")
        try:
            result.append(DailyBar(
                symbol=instrument.symbol, trade_date=day, **prices,
                volume_shares=int(volume) if volume is not None else None, amount_cny=amount,
                tradestatus=tradestatus, is_st=is_st,
                turnover_ratio=turnover / 100 if turnover is not None else None,
                pct_change_ratio=pct_change / 100 if pct_change is not None else None,
                price_unit="CNY" if is_stock else "index_points", quality_flags=flags,
                parameters={"code": instrument.symbol, "start_date": start_date.isoformat(),
                            "end_date": end_date.isoformat(), "frequency": "d", "adjustflag": "3"},
                **_provenance(row, fetched_at, sdk_version),
            ))
        except ValidationError as exc:
            raise DataQualityError(f"{instrument.symbol} {day}: {exc}") from exc
    return sorted(result, key=lambda item: item.trade_date)
