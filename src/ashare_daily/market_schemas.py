"""M1 真实 BaoStock 数据契约。与 M0 人工合成数据完全独立。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SHANGHAI = ZoneInfo("Asia/Shanghai")


class AdjustmentMode(StrEnum):
    """供应商数值标记只在适配器边界出现；M1 限定未复权。"""

    UNADJUSTED = "unadjusted"


class MarketRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    provider: Literal["baostock"] = "baostock"
    fetched_at: datetime
    first_seen_at: datetime
    raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_version: str = Field(min_length=1)
    sdk_version: str = Field(min_length=1)
    parameters: dict[str, str] = Field(default_factory=dict)

    @field_validator("fetched_at", "first_seen_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("时间必须带时区；业务时区为 Asia/Shanghai")
        return value.astimezone(SHANGHAI)

    @model_validator(mode="after")
    def validate_first_seen(self) -> MarketRecord:
        if self.first_seen_at > self.fetched_at:
            raise ValueError("首次观察时间不得晚于抓取时间")
        return self


class Instrument(MarketRecord):
    dataset: Literal["stock_basic"] = "stock_basic"
    symbol: str = Field(pattern=r"^(sh|sz)\.[0-9]{6}$")
    name: str = Field(min_length=1)
    security_type: Literal["stock", "index"]
    exchange: Literal["SH", "SZ"]
    board: Literal["mainboard", "index"]
    ipo_date: date | None
    out_date: date | None
    status: Literal["listed", "delisted"]

    @model_validator(mode="after")
    def validate_identity(self) -> Instrument:
        if self.exchange.lower() != self.symbol[:2]:
            raise ValueError("证券代码与交易所不一致")
        if self.board != ("mainboard" if self.security_type == "stock" else "index"):
            raise ValueError("证券类别与板块不一致")
        if self.ipo_date and self.out_date and self.out_date < self.ipo_date:
            raise ValueError("退市日期早于上市日期")
        return self


class CalendarDay(MarketRecord):
    dataset: Literal["trade_dates"] = "trade_dates"
    calendar_date: date
    is_trading_day: bool


class DailyBar(MarketRecord):
    dataset: Literal["history_k_data_plus"] = "history_k_data_plus"
    symbol: str = Field(pattern=r"^(sh|sz)\.[0-9]{6}$")
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    preclose: Decimal | None
    volume_shares: int | None = Field(ge=0)
    amount_cny: Decimal | None
    tradestatus: bool | None
    is_st: bool | None
    turnover_ratio: Decimal | None = None
    pct_change_ratio: Decimal | None = None
    adjustment_mode: AdjustmentMode = AdjustmentMode.UNADJUSTED
    price_unit: Literal["CNY", "index_points"]
    volume_unit: Literal["shares"] = "shares"
    amount_unit: Literal["CNY"] = "CNY"
    quality_flags: list[str] = Field(default_factory=list)

    @field_validator("volume_shares", mode="before")
    @classmethod
    def whole_shares(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or Decimal(str(value)) % 1 != 0):
            raise ValueError("成交量必须是整数股")
        return value

    @model_validator(mode="after")
    def validate_values(self) -> DailyBar:
        prices = [self.open, self.high, self.low, self.close, self.preclose]
        for value in prices:
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError("有效价格必须是有限正数；缺失值保留为空")
        for name in ("amount_cny", "turnover_ratio", "pct_change_ratio"):
            value = getattr(self, name)
            if value is not None and not value.is_finite():
                raise ValueError(f"{name} 必须是有限数值")
            if name != "pct_change_ratio" and value is not None and value < 0:
                raise ValueError(f"{name} 不能为负数")
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("最高价小于最低价")
        for value in (self.open, self.close):
            if value is not None:
                if self.high is not None and value > self.high:
                    raise ValueError("开盘价或收盘价超过最高价")
                if self.low is not None and value < self.low:
                    raise ValueError("开盘价或收盘价低于最低价")
        if self.tradestatus is False and (
            self.volume_shares not in (None, 0) or self.amount_cny not in (None, Decimal(0))
        ):
            raise ValueError("停牌状态与非零成交量/额冲突")
        return self
