"""Persist every real HTTP attempt before sending; crashes do not refund calls."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
_STATUSES = {"ok", "timeout", "error", "authentication_error", "rate_limited", "refused", "truncated",
             "network_error", "http_error", "invalid_response", "unknown", "completed", "failed"}


class BudgetExceeded(RuntimeError):
    def __init__(self, day: str, used: int, limit: int):
        self.day, self.used, self.limit = day, used, limit
        super().__init__(f"北京时间 {day} 模型请求预算已用 {used}/{limit}；停止新增请求")


def _day(value: str | date) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("预算时间必须包含时区")
        return value.astimezone(SHANGHAI).date().isoformat()
    raw = str(value)
    parsed = date.fromisoformat(raw)
    if parsed.isoformat() != raw:
        raise ValueError("预算日期必须为 YYYY-MM-DD")
    return raw


class BudgetLedger:
    def __init__(self, database_path: Path):
        self.path = Path(database_path).absolute()
        if any(item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction())
               for item in (self.path, *self.path.parents)):
            raise ValueError("预算数据库路径不能经过符号链接或 junction")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS m4_model_reservations (
                reservation_id TEXT PRIMARY KEY, budget_day TEXT NOT NULL, run_id TEXT NOT NULL,
                reserved_at TEXT NOT NULL, status TEXT NOT NULL, usage_json TEXT, updated_at TEXT,
                configured_limit INTEGER NOT NULL CHECK(configured_limit>0))""")
            db.execute("CREATE INDEX IF NOT EXISTS m4_budget_day ON m4_model_reservations(budget_day)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, day: str | date, run_id: str, limit: int) -> str:
        day = _day(day)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("每日模型请求预算必须是正整数")
        if not isinstance(run_id, str) or not run_id or len(run_id) > 160:
            raise ValueError("run_id 无效")
        reservation = uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("SELECT count(*) FROM m4_model_reservations WHERE budget_day=?", (day,)).fetchone()[0]
            if used >= limit:
                raise BudgetExceeded(day, used, limit)
            db.execute("INSERT INTO m4_model_reservations VALUES (?,?,?,?,?,?,?,?)", (
                reservation, day, run_id, datetime.now(SHANGHAI).isoformat(), "reserved", None, None, limit))
        return reservation

    def record_usage(self, reservation_id: str, usage: dict | None, status: str):
        if status not in _STATUSES:
            raise ValueError("未知模型尝试状态；请用 error 并在独立脱敏运行日志记录细节")
        clean = None
        if usage is not None:
            if not isinstance(usage, dict):
                raise ValueError("usage 必须来自供应商计数对象或为 None")
            clean = {key: value for key, value in usage.items()
                     if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                     and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
            clean = clean or None
        with self._connect() as db:
            result = db.execute("UPDATE m4_model_reservations SET usage_json=?,status=?,updated_at=? WHERE reservation_id=?", (
                json.dumps(clean) if clean is not None else None, status,
                datetime.now(SHANGHAI).isoformat(), reservation_id))
            if result.rowcount != 1:
                raise ValueError("不存在的模型预算预留")

    def summary(self, day: str | date) -> dict:
        day = _day(day)
        with self._connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM m4_model_reservations WHERE budget_day=? ORDER BY reserved_at,reservation_id", (day,))]
        totals, reported = {}, 0
        statuses = {}
        for row in rows:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            if row["usage_json"]:
                reported += 1
                for key, value in json.loads(row["usage_json"]).items():
                    totals[key] = totals.get(key, 0) + value
        return {"budget_day": day, "timezone": "Asia/Shanghai", "reserved_attempts": len(rows),
                "requests_with_usage": reported, "provider_reported_usage": totals or None,
                "usage_complete": bool(rows) and reported == len(rows), "status_counts": statuses,
                "currency_cost": None, "cost_note": "未配置可靠价格；仅记录供应商 token 用量"}
