"""Pinned local XSHG schedule, independently reviewed against exchange notices.

This is versioned third-party calendar evidence, not a BaoStock response or a
live exchange observation. Expand the reviewed range only with a new review.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
import hashlib
from importlib.metadata import version
import json
from pathlib import Path

from ashare_daily.market_schemas import SHANGHAI

SCHEMA = "exchange-calendar-cache-v1"
VERSION = "4.13.2"
SOURCE_HASH = "0450145f89c503f7311ebdabfa75177b21cd01aa5ec2dcc54cfc5a23f118336a"
START, END = date(2024, 1, 1), date(2026, 12, 31)
REVIEWED_ON = "2026-10-09"
NOTICES = [
    "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20231226_5733941.shtml",
    "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20241223_10767110.shtml",
    "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml",
]
# Published closure intervals; weekend makeup workdays are NOT market sessions.
HOLIDAYS = {
    2024: [("01-01", "01-01"), ("02-09", "02-17"), ("04-04", "04-06"),
           ("05-01", "05-05"), ("06-10", "06-10"), ("09-15", "09-17"), ("10-01", "10-07")],
    2025: [("01-01", "01-01"), ("01-28", "02-04"), ("04-04", "04-06"),
           ("05-01", "05-05"), ("05-31", "06-02"), ("10-01", "10-08")],
    2026: [("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"),
           ("05-01", "05-05"), ("06-19", "06-21"), ("09-25", "09-27"), ("10-01", "10-07")],
}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@lru_cache(maxsize=1)
def _reviewed_schedule() -> tuple[tuple[str, bool], ...]:
    import exchange_calendars as xcals
    from exchange_calendars import exchange_calendar_xshg

    if version("exchange-calendars") != VERSION:
        raise ValueError("local_calendar_version_unreviewed")
    if hashlib.sha256(Path(exchange_calendar_xshg.__file__).read_bytes()).hexdigest() != SOURCE_HASH:
        raise ValueError("local_calendar_source_hash_mismatch")
    schedule = xcals.get_calendar("XSHG", start=START.isoformat(), end=END.isoformat())
    sessions = {stamp.date() for stamp in schedule.sessions}
    closures = set()
    for year, intervals in HOLIDAYS.items():
        for left, right in intervals:
            first, last = date.fromisoformat(f"{year}-{left}"), date.fromisoformat(f"{year}-{right}")
            closures.update(first + timedelta(days=i) for i in range((last - first).days + 1))
    days = []
    for i in range((END - START).days + 1):
        day = START + timedelta(days=i)
        opened = day in sessions
        # Verify the library against the published calendar, do not generate a
        # guessed schedule when the library or its reviewed bounds disagree.
        if opened != (day.weekday() < 5 and day not in closures):
            raise ValueError("local_calendar_official_notice_conflict:" + day.isoformat())
        days.append((day.isoformat(), opened))
    return tuple(days)


def build_packet(target: date, *, mode: str) -> dict:
    if type(target) is not date or not START <= target <= END:
        raise ValueError("local_calendar_coverage_unreviewed: supported 2024-01-01 through 2026-12-31")
    if mode not in {"research", "offline_test"}:
        raise ValueError("local_calendar_mode_invalid")
    stamp = datetime.now(SHANGHAI).isoformat()
    days = dict(_reviewed_schedule())
    packet = {
        "schema_version": SCHEMA, "provider": "exchange_calendars", "calendar": "XSHG",
        "source_kind": "versioned_local_exchange_calendar", "source_version": VERSION,
        "source_file_sha256": SOURCE_HASH, "reviewed_on": REVIEWED_ON, "notice_urls": list(NOTICES),
        "mode": mode, "verification_kind": "versioned_local_calendar" if mode == "research" else "offline_test",
        "first_seen_at": stamp, "fetched_at": stamp,
        "parameters": {"start_date": START.isoformat(), "end_date": END.isoformat()},
        "calendar_days": days, "raw_hash": digest(days),
    }
    packet["content_hash"] = digest(packet)
    return packet


def verify_packet(packet: dict, *, mode: str) -> tuple[dict, dict[date, bool]]:
    if not isinstance(packet, dict) or mode not in {"research", "offline_test"}:
        raise ValueError("local_calendar_packet_invalid")
    expected = {"schema_version": SCHEMA, "provider": "exchange_calendars", "calendar": "XSHG",
                "source_kind": "versioned_local_exchange_calendar", "source_version": VERSION,
                "source_file_sha256": SOURCE_HASH, "reviewed_on": REVIEWED_ON,
                "notice_urls": NOTICES, "mode": mode,
                "verification_kind": "versioned_local_calendar" if mode == "research" else "offline_test",
                "parameters": {"start_date": START.isoformat(), "end_date": END.isoformat()}}
    if any(packet.get(key) != value for key, value in expected.items()):
        raise ValueError("local_calendar_source_version_mode_or_coverage_invalid")
    if packet.get("content_hash") != digest({k:v for k,v in packet.items() if k != "content_hash"}):
        raise ValueError("local_calendar_packet_hash_invalid")
    stamp = datetime.fromisoformat(packet["fetched_at"])
    if stamp.tzinfo is None or stamp > datetime.now(SHANGHAI) or packet.get("first_seen_at") != packet["fetched_at"]:
        raise ValueError("local_calendar_observation_time_invalid")
    values = packet.get("calendar_days")
    if (not isinstance(values, dict) or any(type(v) is not bool for v in values.values())
            or values != dict(_reviewed_schedule()) or packet.get("raw_hash") != digest(values)):
        raise ValueError("local_calendar_dates_or_hash_invalid")
    metadata = {key: packet[key] for key in ["provider", "source_kind", "source_version", "fetched_at", "raw_hash", "parameters"]}
    return metadata, {date.fromisoformat(day): opened for day, opened in values.items()}


def ensure_packet(target: date, cache_root: Path, *, mode: str) -> Path:
    """Keep the first observation of an existing valid local schedule immutable."""
    if type(target) is not date or not START <= target <= END:
        raise ValueError("local_calendar_coverage_unreviewed: supported 2024-01-01 through 2026-12-31")
    for path in sorted(cache_root.glob("exchange-calendar-*.json")):
        try:
            _, days = verify_packet(json.loads(path.read_text(encoding="utf-8")), mode=mode)
            if target in days:
                return path
        except (ValueError, KeyError, TypeError, OSError):
            continue  # Invalid cache is replaced by a verified local build, never a guessed date.
    packet = build_packet(target, mode=mode)
    verify_packet(packet, mode=mode)
    cache_root.mkdir(parents=True, exist_ok=True)
    path = cache_root / ("exchange-calendar-" + packet["content_hash"] + ".json")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(packet, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return path
