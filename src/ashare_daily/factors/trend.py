"""Exact decimal formulas. Missing points never shorten a trading window."""

from decimal import Decimal, localcontext
from typing import Iterable


def number(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None
    return result if result.is_finite() else None


def mean_window(values: Iterable, window: int, *, positive: bool = True) -> Decimal | None:
    points = list(values)
    if window <= 0:
        raise ValueError("窗口必须为正数")
    if len(points) < window:
        return None
    selected = [number(value) for value in points[-window:]]
    if any(value is None or (value <= 0 if positive else value < 0) for value in selected):
        return None
    with localcontext() as context:
        context.prec = 40
        return sum(selected, Decimal(0)) / Decimal(window)


def period_return(values: Iterable, days: int) -> Decimal | None:
    points = list(values)
    if days <= 0:
        raise ValueError("收益窗口必须为正数")
    if len(points) < days + 1:
        return None
    selected = [number(value) for value in points[-(days + 1):]]
    # Validate all 21 aligned dates, not just endpoints.
    if any(value is None or value <= 0 for value in selected):
        return None
    with localcontext() as context:
        context.prec = 40
        return selected[-1] / selected[0] - Decimal(1)


def subtract(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None:
        return None
    with localcontext() as context:
        context.prec = 40
        return left - right
