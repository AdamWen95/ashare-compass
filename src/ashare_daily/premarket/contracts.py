"""Small validators shared by the premarket domain boundaries."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA = "premarket-brief-v2"
READABLE_SCHEMAS = {"premarket-brief-v1", SCHEMA}
STANCE_VERSION = "premarket-stance-v2"
RECOMMENDATION_VERSION = "premarket-recommendation-v1"


def aware(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("premarket_timezone_required")
    return result.astimezone(SHANGHAI)


def decimal(value, *, positive=False, nonnegative=False) -> Decimal:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("premarket_numeric_missing")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("premarket_numeric_invalid") from None
    if not result.is_finite() or positive and result <= 0 or nonnegative and result < 0:
        raise ValueError("premarket_numeric_invalid")
    return result


def text_number(value: Decimal) -> str:
    return format(value, "f")
