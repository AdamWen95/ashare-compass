"""Read-only 3/5/10-session price observations, never executable returns."""
from datetime import date, timedelta

from .contracts import decimal, text_number


def followup_observations(brief: dict, bars: dict, calendar: dict, *, as_of: str) -> dict:
    anchor, end = date.fromisoformat(brief["market_as_of_date"]), date.fromisoformat(as_of)
    if end < anchor:
        raise ValueError("premarket_study_before_anchor")
    days = []
    current = anchor
    while current <= end:
        value = calendar.get(current.isoformat())
        if type(value) is not bool:
            raise ValueError("premarket_study_calendar_unknown")
        if value and current > anchor:
            days.append(current.isoformat())
        current += timedelta(days=1)
    selected = {r["security_id"] for r in brief["focus_candidates"]}
    baseline = set(brief.get("baseline_ids", []))
    records = []
    for identity in sorted(selected | baseline):
        rows = bars.get(identity, [])
        by_date = {r["trade_date"]: r for r in rows}
        if len(rows) != len(by_date):
            raise ValueError("premarket_study_duplicate_bars")
        for horizon in (3, 5, 10):
            target = days[horizon-1] if len(days) >= horizon else None
            status, value = "not_matured", None
            if target:
                try:
                    first = decimal(by_date[str(anchor)]["close"], positive=True)
                    last = decimal(by_date[target]["close"], positive=True)
                    value = text_number((last/first-1)*100)
                    status = "observed"
                except (KeyError, TypeError, ValueError):
                    status = "missing_data"
            records.append({"security_id": identity, "horizon": horizon, "target_date": target,
                "status": status, "change_pct": value, "in_selected": identity in selected, "in_baseline": identity in baseline})
    return {"measure": "close_change_not_executable_return", "as_of": as_of, "records": records,
            "notice": "基于行情日收盘的后续价格变化；不是可成交回测、账户收益或获利概率"}
