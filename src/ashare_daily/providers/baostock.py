"""BaoStock 只读调用；历史样本与完整名单使用独立的响应契约。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_ROWS = 500
MAX_LOG_CHARS = 4096
HISTORY_INDEX_FIELDS = ["date", "code", "open", "high", "low", "close", "preclose", "volume", "amount"]
HISTORY_STOCK_FIELDS = [*HISTORY_INDEX_FIELDS, "adjustflag", "tradestatus", "isST"]
CALENDAR_FIELDS = ["calendar_date", "is_trading_day"]
BASIC_FIELDS = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
UNIVERSE_FIELDS = ["code", "tradeStatus", "code_name"]
DISCOVERY_OPERATIONS = {"basic_all", "universe"}
NETWORK_CODES = {f"1000200{number}" for number in range(1, 9)}
TIMEOUT_CODES = {"10002003", "10002006", "10002008", "worker_timeout"}
RETRYABLE_CODES = NETWORK_CODES | {"10005001", "worker_timeout", "network_exception"}


class BaoStockAdjustmentFlag(StrEnum):
    """仅表示 BaoStock 自身的复权标记，不与其他来源的值通用。"""

    UNADJUSTED = "3"
    FORWARD_ADJUSTED = "2"


def sdk_version() -> str:
    try:
        return importlib.metadata.version("baostock")
    except importlib.metadata.PackageNotFoundError:
        return "not_installed"


def clean_log(value: Any, limit: int = MAX_LOG_CHARS) -> str:
    """仅保存短日志；本适配器不接受或传递账号、密码、API key。"""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    text = re.sub(r"(?i)\b(password|passwd|api[_-]?key|token|user[_-]?id|username|account)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    text = text.replace("anonymous", "[REDACTED-ACCOUNT]")
    return text[:limit]


def raw_hash(fields: list[str], rows: list[dict[str, str]]) -> str:
    payload = json.dumps({"fields": fields, "rows": rows}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_error(code: str, message: str = "") -> str:
    text = message.casefold()
    if code in {"10001005", "429"} or any(token in text for token in ("rate limit", "too many requests", "限流", "频率限制", "登录数达到上限")):
        return "rate_limited"
    if code in {"10001001", "10001002", "10001006", "10001007", "10001008", "10001009", "10001011", "401", "403"} or any(token in text for token in ("permission denied", "unauthorized", "forbidden", "权限不足", "黑名单", "验证码", "付费")):
        return "permission_denied"
    if code in TIMEOUT_CODES:
        return "timeout"
    if code in NETWORK_CODES or code == "network_exception":
        return "unknown"
    if code == "10001004" or code.startswith("100040") or code in {"schema_changed", "worker_protocol_error"}:
        return "schema_changed"
    return "unknown"


def validate_request(operation: str, parameters: dict[str, Any]) -> dict[str, str]:
    required = {"calendar": {"start_date", "end_date"}, "basic": {"code"}, "history": {"code", "start_date", "end_date", "security_type"}, "history_f2": {"code", "start_date", "end_date", "security_type"}, "basic_all": set(), "universe": {"day"}}
    if operation not in required:
        raise ValueError("只允许 calendar、basic、history、basic_all、universe 只读操作")
    allowed = required[operation] | ({"adjustment_mode"} if operation in {"history", "history_f2"} else set())
    if set(parameters) - allowed or required[operation] - set(parameters):
        raise ValueError("请求参数缺失或含不允许的字段")
    if any(not isinstance(value, str) for value in parameters.values()):
        raise ValueError("请求参数必须为字符串")
    result = dict(parameters)
    if "code" in result and re.fullmatch(r"(?:sh|sz)\.\d{6}", result["code"]) is None:
        raise ValueError("证券代码必须明确使用 sh.000001 或 sz.000001 格式")
    if operation in {"calendar", "history", "history_f2"}:
        values = []
        for name in ("start_date", "end_date"):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", result[name]):
                raise ValueError("日期必须使用 YYYY-MM-DD 格式")
            values.append(date.fromisoformat(result[name]))
        max_days = 730 if operation == "history_f2" else 365
        if not 0 <= (values[1] - values[0]).days <= max_days:
            raise ValueError(f"单次请求须按顺序且最多包含 {max_days + 1} 个自然日")
    if operation == "universe":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", result["day"]):
            raise ValueError("日期必须使用 YYYY-MM-DD 格式")
        date.fromisoformat(result["day"])
    if operation in {"history", "history_f2"}:
        if result["security_type"] not in {"stock", "index"}:
            raise ValueError("security_type 只允许 stock 或 index")
        result.setdefault("adjustment_mode", "unadjusted")
        allowed_modes = {"unadjusted", "forward_adjusted"} if result["security_type"] == "stock" else {"unadjusted"}
        if result["adjustment_mode"] not in allowed_modes:
            raise ValueError("股票允许 unadjusted/forward_adjusted；指数只允许 unadjusted 原生点位")
    return result


def expected_fields(operation: str, parameters: dict[str, str]) -> list[str]:
    if operation == "calendar":
        return CALENDAR_FIELDS.copy()
    if operation in {"basic", "basic_all"}:
        return BASIC_FIELDS.copy()
    if operation == "universe":
        return UNIVERSE_FIELDS.copy()
    return (HISTORY_INDEX_FIELDS if parameters["security_type"] == "index" else HISTORY_STOCK_FIELDS).copy()


def base_result(operation: str, parameters: dict[str, str]) -> dict[str, Any]:
    return {
        "ok": False, "status": "unknown", "error_code": "not_started", "error_msg": "尚未完成请求",
        "fields": [], "rows": [], "fetched_at": datetime.now(SHANGHAI).isoformat(),
        "elapsed_seconds": 0.0, "sdk_version": sdk_version(), "parameters": parameters,
        "operation": operation, "raw_hash": raw_hash([], []),
        "login": {"ok": False, "error_code": "not_completed", "error_msg": "登录尚未完成"},
        "attempts": [], "sdk_log": "",
    }


def parse_progress(stdout: str | bytes | None) -> tuple[list[dict[str, Any]], bool]:
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    events = []
    invalid = False
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("event") not in {"login", "query", "final", "stage"}:
                invalid = True
            else:
                events.append(event)
        except (ValueError, TypeError):
            invalid = True
    return events, invalid


class BaoStockClient:
    def __init__(self, timeout_seconds: float = 20, max_attempts: int = 2, pause_seconds: float = 0.5, *, diagnostic_stages: bool = False):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ValueError("timeout_seconds 必须介于 0（不含）与 120 秒之间")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts 必须为 1 至 3")
        if isinstance(pause_seconds, bool) or not isinstance(pause_seconds, (int, float)) or not math.isfinite(pause_seconds) or not 0 <= pause_seconds <= 30:
            raise ValueError("pause_seconds 必须介于 0 与 30 秒之间")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.pause_seconds = pause_seconds
        if not isinstance(diagnostic_stages, bool):
            raise ValueError("diagnostic_stages 必须为布尔值")
        self.diagnostic_stages = diagnostic_stages

    def query(self, operation: str, **parameters: str) -> dict[str, Any]:
        parameters = validate_request(operation, parameters)
        attempts: list[dict[str, Any]] = []
        started = time.monotonic()
        result = base_result(operation, parameters)
        for number in range(1, self.max_attempts + 1):
            result = self._attempt(operation, parameters)
            attempts.append({
                "attempt": number, "ok": result["ok"], "status": result["status"],
                "error_code": result["error_code"], "error_msg": result["error_msg"],
                "fetched_at": result["fetched_at"], "elapsed_seconds": result["elapsed_seconds"],
                "login": result["login"], "sdk_log": result["sdk_log"],
            })
            if "diagnostics" in result:
                attempts[-1]["diagnostics"] = result["diagnostics"]
            if result["ok"] or result["status"] in {"permission_denied", "rate_limited", "schema_changed"}:
                break
            if result["error_code"] not in RETRYABLE_CODES or number == self.max_attempts:
                break
            time.sleep(self.pause_seconds * number)
        result["attempts"] = attempts
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)
        return result

    def _attempt(self, operation: str, parameters: dict[str, str]) -> dict[str, Any]:
        result = base_result(operation, parameters)
        started = time.monotonic()
        # Windows 的默认 cp936 不等于父进程解码的 UTF-8；显式设定子解释器，
        # 不依赖 PowerShell code page 或 PYTHONUTF8 环境变量。
        command = [sys.executable, "-X", "utf8", "-m", "ashare_daily.providers.baostock_worker"]
        request: dict[str, Any] = {"operation": operation, "parameters": parameters}
        if self.diagnostic_stages:
            request["diagnostic_stages"] = True
        payload = json.dumps(request, ensure_ascii=False)
        kwargs: dict[str, Any] = {"input": payload, "text": True, "encoding": "utf-8", "errors": "replace", "capture_output": True, "timeout": self.timeout_seconds, "check": False}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        stdout: str | bytes | None = ""
        stderr: str | bytes | None = ""
        timeout = False
        returncode = None
        try:
            completed = subprocess.run(command, **kwargs)
            stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired as exc:
            # subprocess.run 已 kill/wait 子进程；保留超时前已冲刷的登录进度。
            stdout, stderr, timeout = exc.stdout, exc.stderr, True
        except OSError as exc:
            result.update(error_code="worker_start_failed", error_msg=clean_log(f"{type(exc).__name__}: {exc}"))
        events, invalid = parse_progress(stdout)
        for event in events:
            if event["event"] == "login" and isinstance(event.get("login"), dict):
                result["login"] = event["login"]
        stage_events = [event for event in events if event["event"] == "stage"]
        final_events = [event for event in events if event["event"] == "final"]
        if timeout:
            result.update(status="timeout", error_code="worker_timeout", error_msg=f"独立子进程超过 {self.timeout_seconds:g} 秒，已终止；不视为成功或无数据。")
        elif returncode is not None:
            if invalid or returncode != 0 or len(final_events) != 1 or not isinstance(final_events[0].get("result"), dict):
                result.update(status="schema_changed", error_code="worker_protocol_error", error_msg="worker 未返回唯一有效的 final JSON，拒绝使用结果。")
            else:
                candidate = final_events[0]["result"]
                if self._valid_worker_result(candidate, operation, parameters):
                    result = candidate
                else:
                    result.update(status="schema_changed", error_code="worker_protocol_error", error_msg="worker 结果不符合字段、原始字符串、时间或哈希契约。")
        result["sdk_log"] = clean_log(stderr)
        if self.diagnostic_stages:
            # Even a killed worker has flushed the stage immediately before the
            # blocking SDK call. This is observation, never partial list data.
            active: list[str] = []
            for event in stage_events:
                if event.get("state") == "started":
                    active.append(event.get("stage", "unknown"))
                elif event.get("state") == "completed" and event.get("stage") in active:
                    active.remove(event["stage"])
            query_response_failed = any(
                event.get("stage") == "query_wait" and event.get("state") == "completed"
                and isinstance(event.get("error_code"), str) and event["error_code"] != "0"
                for event in stage_events
            )
            result["diagnostics"] = {
                "schema_version": "f11-baostock-stages-v1", "events": stage_events,
                "interrupted_stage": active[-1] if timeout and active else None,
                "failure_stage": ("query_wait" if query_response_failed else active[-1] if active else None) if not result["ok"] else None,
                "worker_exit_code": returncode, "hard_timeout": timeout,
            }
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)
        if not result["ok"]:
            result["fetched_at"] = datetime.now(SHANGHAI).isoformat()
        return result

    @staticmethod
    def _valid_worker_result(result: dict[str, Any], operation: str, parameters: dict[str, str]) -> bool:
        try:
            required = set(base_result(operation, parameters))
            if not required <= set(result) or result["operation"] != operation or result["parameters"] != parameters:
                return False
            stamp = datetime.fromisoformat(result["fetched_at"])
            if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 8 * 3600:
                return False
            fields, rows = result["fields"], result["rows"]
            if not isinstance(result["ok"], bool) or not isinstance(fields, list) or not isinstance(rows, list):
                return False
            if operation not in DISCOVERY_OPERATIONS and len(rows) > MAX_ROWS:
                return False
            if result["ok"] and operation in DISCOVERY_OPERATIONS:
                pagination = result.get("pagination", {})
                pages = pagination.get("pages", [])
                if pagination.get("exhausted") is not True or pagination.get("observed_records") != len(rows) or not pages:
                    return False
                if [page.get("page") for page in pages] != list(range(1, len(pages) + 1)):
                    return False
                size = pagination.get("page_size")
                if type(size) is not int or size <= 0 or any(type(page.get("records")) is not int or not 0 <= page["records"] <= size for page in pages):
                    return False
                if any(page["records"] != size for page in pages[:-1]) or pages[-1]["records"] >= size or sum(page["records"] for page in pages) != len(rows):
                    return False
            login = result["login"]
            if not isinstance(login, dict) or set(login) != {"ok", "error_code", "error_msg"} or not isinstance(login["ok"], bool) or not isinstance(login["error_code"], str) or not isinstance(login["error_msg"], str):
                return False
            if not isinstance(result["error_code"], str) or not isinstance(result["error_msg"], str) or result["status"] not in {"ok", "empty_confirmed", "stale", "partial", "permission_denied", "rate_limited", "schema_changed", "timeout", "unknown"}:
                return False
            if result["ok"] and (not login["ok"] or login["error_code"] != "0"):
                return False
            if any(not isinstance(field, str) for field in fields) or len(set(fields)) != len(fields):
                return False
            if result["ok"] and (set(fields) != set(expected_fields(operation, parameters)) or result["error_code"] != "0" or result["status"] != ("ok" if rows else "empty_confirmed")):
                return False
            if not result["ok"] and rows:
                return False
            if any(not isinstance(row, dict) or set(row) != set(fields) or any(not isinstance(value, str) for value in row.values()) for row in rows):
                return False
            return result["raw_hash"] == raw_hash(fields, rows)
        except (KeyError, ValueError, TypeError, AttributeError):
            return False
