from datetime import date, datetime

import pytest


CALENDAR = {
    "2026-10-08": True, "2026-10-09": True, "2026-10-10": False,
    "2026-10-11": False, "2026-10-12": True, "2026-10-13": True,
}


def resolve(stamp="2026-10-12T08:30:00+08:00", calendar=None, **kwargs):
    from ashare_daily.premarket.timing import resolve_schedule
    return resolve_schedule(datetime.fromisoformat(stamp), CALENDAR if calendar is None else calendar, **kwargs)


def test_monday_uses_friday_close_and_monday_target():
    result = resolve()
    assert result["report_date"] == result["target_session_date"] == "2026-10-12"
    assert result["market_as_of_date"] == "2026-10-09"
    assert result["news_cutoff_at"] == "2026-10-12T08:55:00+08:00"
    assert result["publication_due_at"] == "2026-10-12T09:00:00+08:00"
    assert result["is_trading_day"] is True


def test_sunday_has_own_edition_and_next_session():
    result = resolve("2026-10-11T08:30:00+08:00")
    assert result["report_date"] == "2026-10-11"
    assert result["target_session_date"] == "2026-10-12"
    assert result["market_as_of_date"] == "2026-10-09"
    assert result["is_trading_day"] is False


@pytest.mark.parametrize("stamp, want", [
    ("2026-10-12T08:29:59+08:00", "not_due"),
    ("2026-10-12T08:30:00+08:00", "on_time"),
    ("2026-10-12T09:00:00+08:00", "late"),
    ("2026-10-12T09:14:59+08:00", "late"),
    ("2026-10-12T09:15:00+08:00", "missed"),
])
def test_scheduled_boundaries(stamp, want):
    assert resolve(stamp)["timing_status"] == want


@pytest.mark.parametrize("calendar", [
    {"2026-12-30": True, "2026-12-31": False},
    {"2026-10-09": True, "2026-10-12": True},
    {**CALENDAR, "2026-10-11": 0},
])
def test_missing_or_untrusted_calendar_is_not_weekday_guess(calendar):
    with pytest.raises(ValueError, match="calendar"):
        resolve("2026-12-31T08:30:00+08:00" if "2026-12-31" in calendar else "2026-10-12T08:30:00+08:00", calendar)


def test_naive_clock_and_future_report_rejected():
    with pytest.raises(ValueError, match="timezone"):
        resolve("2026-10-12T08:30:00")
    with pytest.raises(ValueError, match="future"):
        resolve(report_date=date(2026, 10, 13))


def test_stage_deadline_does_not_restart_budget():
    from ashare_daily.premarket.timing import stage_seconds
    schedule = resolve()
    assert stage_seconds(schedule, "market", datetime.fromisoformat("2026-10-12T08:49:30+08:00")) == 30
    assert stage_seconds(schedule, "market", datetime.fromisoformat("2026-10-12T08:50:01+08:00")) == 0
    assert stage_seconds(schedule, "news", datetime.fromisoformat("2026-10-12T08:54:00+08:00")) == 60


def test_news_window_is_bounded_and_marks_gap():
    from ashare_daily.premarket.timing import request_window
    schedule = resolve()
    assert request_window(schedule, None)["start_at"] == "2026-10-09T08:55:00+08:00"
    result = request_window(schedule, "2026-09-01T08:55:00+08:00")
    assert result["start_at"] == "2026-09-28T08:55:00+08:00"
    assert result["coverage_gap"] is True
    with pytest.raises(ValueError, match="cutoff"):
        request_window(schedule, "2026-10-13T08:55:00+08:00")
