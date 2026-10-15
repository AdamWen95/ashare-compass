"""Separate edition/session/close dates using an already verified calendar."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import math

from .contracts import SHANGHAI, aware


def universe_budget_seconds(available: float, maximum: int = 900, *, reserve: float = 0) -> float:
    """Elapsed seconds only; never extend a phase deadline or a request quota."""
    if (type(maximum) is not int or not 1 <= maximum <= 900
            or type(available) not in {int, float} or not math.isfinite(available) or available < 0
            or type(reserve) not in {int, float} or not math.isfinite(reserve) or reserve < 0):
        raise ValueError("premarket_universe_time_budget_invalid")
    return min(maximum, max(0, available - reserve))


def resolve_schedule(now: datetime, calendar: dict[str, bool], *, report_date: date | None = None) -> dict:
    now = aware(now)
    day = report_date or now.date()
    if type(day) is not date or day > now.date():
        raise ValueError("premarket_future_report_date")
    if not isinstance(calendar, dict) or not calendar or len(calendar) > 3660:
        raise ValueError("premarket_calendar_invalid")
    days = {}
    try:
        for key, opened in calendar.items():
            parsed = date.fromisoformat(key)
            if parsed.isoformat() != key or type(opened) is not bool:
                raise ValueError()
            days[parsed] = opened
    except (ValueError, TypeError):
        raise ValueError("premarket_calendar_invalid") from None
    dates = sorted(days)
    if any(right - left != timedelta(days=1) for left, right in zip(dates, dates[1:])) or day not in days:
        raise ValueError("premarket_calendar_coverage_gap")
    previous = [d for d in dates if d < day and days[d]]
    upcoming = [d for d in dates if d >= day and days[d]]
    if not previous or not upcoming:
        raise ValueError("premarket_calendar_session_unknown")
    def clock(hour, minute):
        return datetime.combine(day, time(hour, minute), SHANGHAI)
    status = ("not_due" if now < clock(8, 30) else "on_time" if now < clock(9, 0)
              else "late" if now < clock(9, 15) else "missed")
    return {
        "report_date": day.isoformat(), "target_session_date": upcoming[0].isoformat(),
        "market_as_of_date": previous[-1].isoformat(), "is_trading_day": days[day],
        "timezone": "Asia/Shanghai", "planned_start_at": clock(8, 30).isoformat(),
        "market_deadline_at": clock(8, 50).isoformat(), "news_cutoff_at": clock(8, 55).isoformat(),
        "decision_deadline_at": clock(8, 59).isoformat(), "publication_due_at": clock(9, 0).isoformat(),
        "late_deadline_at": clock(9, 15).isoformat(), "timing_status": status,
        "historical_run": day < now.date(),
    }


def request_window(schedule: dict, previous_cutoff: str | None) -> dict:
    end = aware(schedule["news_cutoff_at"])
    desired = aware(previous_cutoff) if previous_cutoff else end - timedelta(days=3)
    if desired >= end:
        raise ValueError("premarket_previous_cutoff_invalid")
    start = max(desired, end - timedelta(days=14))
    return {"start_at": start.isoformat(), "cutoff_at": end.isoformat(), "coverage_gap": start > desired}


def stage_seconds(schedule: dict, stage: str, now: datetime) -> float:
    key = {"market": "market_deadline_at", "news": "news_cutoff_at",
           "decision": "decision_deadline_at", "publication": "publication_due_at"}.get(stage)
    if key is None:
        raise ValueError("premarket_stage_invalid")
    return max(0.0, (aware(schedule[key]) - aware(now)).total_seconds())
