"""Verified trading calendar resolution with immutable, provenance checked cache.

An unavailable calendar and an observed closed date are different outcomes.
The requested date is never silently replaced with the preceding session.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ashare_daily.providers.baostock import BaoStockClient, SHANGHAI, validate_request
from ashare_daily.quality.baostock import normalize_calendar
from ashare_daily.market_schemas import CalendarDay


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _verified_rows(response: dict[str, Any]) -> dict[date, bool]:
    if response.get("operation") != "calendar" or response.get("ok") is not True:
        raise ValueError("源未成功完成日历请求")
    parameters = response["parameters"]
    validate_request("calendar", parameters)
    if not BaoStockClient._valid_worker_result(response, "calendar", parameters):
        raise ValueError("日历响应参数、哈希或来源时间不符合契约")
    if datetime.fromisoformat(response["fetched_at"]) > datetime.now(SHANGHAI):
        raise ValueError("日历来源获取时间不能在未来")
    start, end = (date.fromisoformat(parameters[key]) for key in ("start_date", "end_date"))
    days = normalize_calendar(response["rows"], start_date=start, end_date=end,
                              fetched_at=datetime.fromisoformat(response["fetched_at"]),
                              sdk_version=response["sdk_version"])
    return {day.calendar_date: day.is_trading_day for day in days}


def _legacy_calendar(database: Path, requested_date: date, mode: str) -> dict[str, Any] | None:
    """Read one existing M1 calendar date without migrating or writing its DB."""
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM market_metadata"))
        if metadata.get("mode") != "research" or metadata.get("provider") != "baostock" or metadata.get("schema_version") != "m1-baostock-market-v1":
            raise ValueError("旧数据库不是已验证的 M1 research 日历来源")
        if metadata.get("verification_kind") != ("live_network" if mode == "research" else "offline_test"):
            raise ValueError("旧数据库真实/测试来源未验证或不匹配")
        row = connection.execute("SELECT calendar_date,is_trading_day,raw_hash,payload_json FROM trading_calendar WHERE provider='baostock' AND calendar_date=?", (requested_date.isoformat(),)).fetchone()
    if row is None:
        return None
    day = CalendarDay.model_validate_json(row[3])
    raw = {"calendar_date": day.calendar_date.isoformat(), "is_trading_day": "1" if day.is_trading_day else "0"}
    if day.calendar_date != requested_date or row[0] != requested_date.isoformat() or row[1] != int(day.is_trading_day) or row[2] != day.raw_hash or day.raw_hash != _digest(raw) or day.content_version != "sha256:" + day.raw_hash:
        raise ValueError("旧数据库日历列值、来源内容哈希或版本不一致")
    start, end = (date.fromisoformat(day.parameters[key]) for key in ("start_date", "end_date"))
    if not start <= requested_date <= end or day.fetched_at > datetime.now(SHANGHAI):
        raise ValueError("旧数据库日历有效范围或来源获取时间无效")
    return {"calendar_days": {requested_date.isoformat(): day.is_trading_day},
            "source_response_hash": day.raw_hash, "source_fetched_at": day.fetched_at.isoformat(),
            "source_first_seen_at": day.first_seen_at.isoformat(), "source_range": day.parameters,
            "source_kind": "legacy_sqlite_calendar_row", "source_record": day.model_dump(mode="json")}


def resolve_calendar(requested_date: date, cache_root: Path, *, client: Any = None,
                     legacy_database: Path | None = None, mode: str = "research") -> dict[str, Any]:
    """Fetch the 40-day range ending at requested_date, or use verified cache.

    Cache has a source response hash and an envelope hash; only a complete
    calendar whose source parameters cover the exact target can be used.
    The call does not read credentials or contact an alternative provider.
    """
    if not isinstance(requested_date, date) or isinstance(requested_date, datetime):
        raise ValueError("requested_date 须为 Asia/Shanghai 下已明确的 date")
    cache_root = Path(cache_root)
    if mode not in {"research", "offline_test"}:
        raise ValueError("日历模式只允许 research 或 offline_test")
    if client is not None and not isinstance(client, BaoStockClient) and mode != "offline_test":
        raise ValueError("注入日历客户端必须显式标记 offline_test")
    if mode == "offline_test" and "research" in {part.casefold() for part in cache_root.parts}:
        raise ValueError("离线日历缓存不能写入 research 路径")
    started_at = datetime.now(SHANGHAI).isoformat()
    start = requested_date - timedelta(days=40)
    response = (client or BaoStockClient()).query("calendar", start_date=start.isoformat(), end_date=requested_date.isoformat())
    output: dict[str, Any] = {
        "status": "calendar_unverified", "calendar_verified": False,
        "requested_date": requested_date.isoformat(), "resolved_trade_date": None,
        "started_at": started_at, "completed_at": None, "provider": "baostock",
        "cached": False, "cache_path": None, "response": response,
        "cache_rejections": [], "validation_error": None,
        "verification_kind": "online" if mode == "research" else "offline_test",
    }
    selected = response
    days: dict[date, bool] = {}
    try:
        days = _verified_rows(response)
        if requested_date not in days:
            raise ValueError("响应未覆盖目标日期")
        packet = {"schema_version": "f1-calendar-cache-v1", "provider": "baostock", "mode": mode,
                  "first_seen_at": response["fetched_at"], "response": response}
        packet["content_hash"] = _digest(packet)
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_path = cache_root / f"{start}_{requested_date}_{packet['content_hash']}.json"
        if not cache_path.exists():
            with cache_path.open("x", encoding="utf-8") as stream:
                json.dump(packet, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        output["cache_path"] = str(cache_path)
    except (ValueError, KeyError, TypeError) as exc:
        days = {}
        output["validation_error"] = str(exc)
        # Permission/limit rejection stops further requests, but reading an
        # existing licensed local calendar does not create a new request.
        candidates = []
        for path in cache_root.glob("*.json"):
            try:
                packet = json.loads(path.read_text(encoding="utf-8"))
                signature = packet.pop("content_hash")
                if signature != _digest(packet) or packet.get("schema_version") != "f1-calendar-cache-v1" or packet.get("provider") != "baostock" or packet.get("mode") != mode:
                    raise ValueError("缓存来源或版本/哈希无效")
                candidate = packet["response"]
                if packet.get("first_seen_at") != candidate["fetched_at"]:
                    raise ValueError("缓存首次观察时间与冻结源响应不一致")
                candidate_days = _verified_rows(candidate)
                if requested_date not in candidate_days:
                    continue
                candidates.append((datetime.fromisoformat(candidate["fetched_at"]), str(path), candidate, candidate_days))
            except (ValueError, KeyError, TypeError, OSError) as cache_error:
                output["cache_rejections"].append({"path": str(path), "reason": str(cache_error)})
        if candidates:
            latest_stamp = max(item[0] for item in candidates)
            newest = [item for item in candidates if item[0] == latest_stamp]
            # A same-time revision conflict is not resolved by filename or
            # random hash order. Compare all overlapping dates before use.
            combined = {}
            conflict = False
            for _, _, _, candidate_days in newest:
                for day, value in candidate_days.items():
                    if day in combined and combined[day] != value:
                        conflict = True
                    combined[day] = value
            if conflict:
                output["validation_error"] = "calendar_cache_conflict: 同一来源获取时间的有效日历缓存互相矛盾"
                output["cache_conflict"] = True
                output["cache_rejections"].extend({"path": item[1], "reason": output["validation_error"]} for item in newest)
            else:
                _, path, selected, days = min(newest, key=lambda item: item[1])
                output.update(cached=True, cache_path=path)
    if requested_date in days:
        trading = days[requested_date]
        output.update(calendar_verified=True, status="verified" if trading else "non_trading_day",
                      resolved_trade_date=requested_date.isoformat() if trading else None,
                      source_response_hash=selected["raw_hash"], source_fetched_at=selected["fetched_at"],
                      source_range=selected["parameters"], calendar_days={key.isoformat(): value for key, value in days.items()})
    elif not output.get("cache_conflict") and legacy_database is not None and Path(legacy_database).is_file():
        try:
            legacy = _legacy_calendar(Path(legacy_database), requested_date, mode)
            if legacy is not None:
                trading = legacy["calendar_days"][requested_date.isoformat()]
                output.update(legacy)
                output.update(calendar_verified=True, status="verified" if trading else "non_trading_day",
                              resolved_trade_date=requested_date.isoformat() if trading else None,
                              cached=True, cache_path=str(legacy_database))
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            output["cache_rejections"].append({"path": str(legacy_database), "reason": str(exc)})
    output["completed_at"] = datetime.now(SHANGHAI).isoformat()
    return output
