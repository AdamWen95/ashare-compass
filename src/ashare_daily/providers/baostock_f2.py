"""F2 serial BaoStock sessions and provenance-checked 320-session calendar.

The SDK has no historical adjustment-as-of parameter. A forward-adjusted
window is one complete provider-current version, never yesterday plus today.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Any

from ashare_daily.calendar import _digest, _verified_rows
from .baostock import BaoStockClient, MAX_LOG_CHARS, SHANGHAI, base_result, clean_log, parse_progress, validate_request
from .baostock_worker import BoundedSDKLog, execute_request


SESSION_OPERATIONS = {"calendar", "history_f2"}


class BaoStockF2Client(BaoStockClient):
    """One serial SDK process, bounded per-request deadline, finite reconnect.

    Parent timeouts kill the process, so a hung socket cannot retain a lock or
    leak into another request. Permissions/rate limits latch until close/new
    client. Concurrent calls are rejected; no multi-session source concurrency.
    """

    def __init__(self, timeout_seconds: float = 20, max_attempts: int = 2, pause_seconds: float = 0.5,
                 *, max_requests_per_session: int = 200, diagnostic_stages: bool = True):
        super().__init__(timeout_seconds, max_attempts, pause_seconds, diagnostic_stages=diagnostic_stages)
        if type(max_requests_per_session) is not int or not 1 <= max_requests_per_session <= 1000:
            raise ValueError("max_requests_per_session 必须介于 1 与 1000")
        self.max_requests_per_session = max_requests_per_session
        self._process = None
        self._events = None
        self._stderr: list[str] = []
        self._request_id = 0
        self._session_requests = 0
        self._busy = threading.Lock()
        self._stopped: dict[str, Any] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def query(self, operation: str, **parameters: str) -> dict[str, Any]:
        if operation not in SESSION_OPERATIONS:
            raise ValueError("F2 串行会话仅接受 calendar/history_f2")
        parameters = validate_request(operation, parameters)
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("BaoStock F2 SDK 会话禁止并发查询")
        try:
            if self._stopped is not None:
                result = base_result(operation, parameters)
                result.update(status=self._stopped["status"], error_code=self._stopped["error_code"],
                              error_msg="本客户端此前收到权限/限流拒绝，已停止该源；未发送请求。",
                              source_stopped=True)
                return result
            result = super().query(operation, **parameters)
            if result["status"] in {"permission_denied", "rate_limited"}:
                self._stopped = result
                self._terminate()
            return result
        finally:
            self._busy.release()

    def _start(self) -> None:
        self._terminate()
        options = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                   "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "ashare_daily.providers.baostock_f2", "--session-worker"], **options)
        self._events, self._stderr, self._session_requests = queue.Queue(), [], 0
        process, events, errors = self._process, self._events, self._stderr

        def read_stdout():
            try:
                for line in process.stdout:
                    if len(line) > 4_000_000:
                        events.put({"protocol_error": "worker JSON 超过安全长度"})
                        return
                    events.put(line)
            finally:
                events.put(None)

        def read_stderr():
            for line in process.stderr:
                if sum(map(len, errors)) < MAX_LOG_CHARS:
                    errors.append(clean_log(line, MAX_LOG_CHARS - sum(map(len, errors))))

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()

    def _terminate(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

    def close(self) -> None:
        # EOF asks the worker to logout. A broken connection is still bounded.
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
        self._terminate()

    def _attempt(self, operation: str, parameters: dict[str, str]) -> dict[str, Any]:
        result, started = base_result(operation, parameters), time.monotonic()
        stage_events: list[dict[str, Any]] = []
        try:
            if self._process is None or self._process.poll() is not None or self._session_requests >= self.max_requests_per_session:
                self.close()
                self._start()
            self._request_id += 1
            request_id = self._request_id
            self._process.stdin.write(json.dumps({"request_id": request_id, "operation": operation, "parameters": parameters}, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
            while True:
                remaining = self.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise queue.Empty
                message = self._events.get(timeout=remaining)
                if message is None:
                    raise ConnectionError("SDK worker 在完整响应前退出")
                if isinstance(message, dict):
                    raise ValueError(message.get("protocol_error", "worker protocol error"))
                events, invalid = parse_progress(message)
                if invalid or len(events) != 1 or events[0].get("request_id") != request_id:
                    raise ValueError("SDK worker 事件协议或请求 ID 不匹配")
                event = events[0]
                if event["event"] == "login":
                    result["login"] = event["login"]
                elif event["event"] == "stage":
                    stage_events.append(event)
                elif event["event"] == "final":
                    candidate = event.get("result")
                    if not isinstance(candidate, dict) or not self._valid_worker_result(candidate, operation, parameters):
                        raise ValueError("SDK worker 响应日期/参数/字段/哈希契约失败")
                    result = candidate
                    self._session_requests += 1
                    break
        except queue.Empty:
            result.update(status="timeout", error_code="worker_timeout", error_msg=f"单次会话请求超过 {self.timeout_seconds:g} 秒，已终止 SDK 进程。")
        except (OSError, ConnectionError) as exc:
            result.update(status="unknown", error_code="network_exception", error_msg=clean_log(f"{type(exc).__name__}: {exc}"))
        except (ValueError, TypeError, KeyError) as exc:
            result.update(status="schema_changed", error_code="worker_protocol_error", error_msg=clean_log(f"{type(exc).__name__}: {exc}"))
        active = []
        for event in stage_events:
            if event.get("state") == "started":
                active.append(event["stage"])
            elif event.get("state") == "completed" and event.get("stage") in active:
                active.remove(event["stage"])
        query_error = any(e.get("stage") == "query_wait" and e.get("state") == "completed" and isinstance(e.get("error_code"), str) and e["error_code"] != "0" for e in stage_events)
        if self.diagnostic_stages:
            result["diagnostics"] = {"schema_version": "f2-baostock-session-v1", "events": stage_events,
                "failure_stage": ("query_wait" if query_error else active[-1] if active else None) if not result["ok"] else None,
                "hard_timeout": result["error_code"] == "worker_timeout", "request_id": self._request_id}
        if not result["ok"]:
            result["sdk_log"] = clean_log(result.get("sdk_log", "") + "".join(self._stderr))
            result["fetched_at"] = datetime.now(SHANGHAI).isoformat()
            self._terminate()
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)
        return result


def resolve_history_calendar(target: date, cache_root: Path, *, history_days: int = 320,
                             client: Any = None, mode: str = "research") -> dict[str, Any]:
    """Use verified F1 cache packets, fetch at most two missing 366-day ranges."""
    if type(target) is not date or type(history_days) is not int or not 1 <= history_days <= 500:
        raise ValueError("target 必须是 date，history_days 必须介于 1 与 500")
    if mode not in {"research", "offline_test"} or (client is not None and not isinstance(client, BaoStockClient) and mode != "offline_test"):
        raise ValueError("真实日历客户端/测试模式不匹配")
    cache_root = Path(cache_root)
    if mode == "offline_test" and "research" in {part.casefold() for part in cache_root.parts}:
        raise ValueError("测试日历不可写入 research 路径")
    output = {"schema_version": "f2-history-calendar-v1", "status": "calendar_unverified", "verified": False,
              "calendar_verified": False, "requested_date": target.isoformat(), "resolved_trade_date": None,
              "history_days": history_days, "calendar": {}, "trading_dates": [], "responses": [],
              "cache_rejections": [], "mode": mode, "verification_kind": "live_network" if mode == "research" else "offline_test"}
    packets: list[tuple[datetime, str, dict, dict]] = []
    source = client or BaoStockClient()

    def add(response: dict, path: str) -> None:
        days = _verified_rows(response)
        packets.append((datetime.fromisoformat(response["fetched_at"]), path, response, days))

    for path in cache_root.glob("*.json"):
        try:
            packet = json.loads(path.read_text(encoding="utf-8"))
            signature = packet.pop("content_hash")
            if signature != _digest(packet) or packet.get("schema_version") != "f1-calendar-cache-v1" or packet.get("provider") != "baostock" or packet.get("mode") != mode or packet.get("first_seen_at") != packet["response"]["fetched_at"]:
                raise ValueError("缓存来源/模式/哈希无效")
            add(packet["response"], str(path))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            output["cache_rejections"].append({"path": str(path), "reason": str(exc)})

    def combined():
        selected = {}
        for stamp, _, _, days in packets:
            for day, value in days.items():
                previous = selected.get(day)
                if previous and previous[0] == stamp and previous[1] != value:
                    raise ValueError("同一来源获取时间的日历缓存冲突")
                if not previous or stamp > previous[0]:
                    selected[day] = (stamp, value)
        return {day: value for day, (_, value) in selected.items()}

    def chosen(days):
        trading = sorted(day for day, value in days.items() if value and day <= target)
        if days.get(target) is not True or len(trading) < history_days:
            return None
        window = trading[-history_days:]
        if (target - window[0]).days > 730 or any(window[0] + timedelta(days=i) not in days for i in range((target - window[0]).days + 1)):
            return None
        return window

    try:
        days = combined()
        for index in range(2):
            if chosen(days) is not None:
                break
            end = target - timedelta(days=366 * index)
            start = end - timedelta(days=365)
            if all(start + timedelta(days=i) in days for i in range(366)):
                continue
            response = source.query("calendar", start_date=start.isoformat(), end_date=end.isoformat())
            record = {"response": response, "path": None, "cached": False}
            output["responses"].append(record)
            verified = _verified_rows(response)
            packet = {"schema_version": "f1-calendar-cache-v1", "provider": "baostock", "mode": mode,
                      "first_seen_at": response["fetched_at"], "response": response}
            packet["content_hash"] = _digest(packet)
            cache_root.mkdir(parents=True, exist_ok=True)
            path = cache_root / f"{start}_{end}_{packet['content_hash']}.json"
            if not path.exists():
                with path.open("x", encoding="utf-8") as stream:
                    json.dump(packet, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
            record["path"] = str(path)
            packets.append((datetime.fromisoformat(response["fetched_at"]), str(path), response, verified))
            days = combined()
            if days.get(target) is False:
                output["status"] = "non_trading_day"
                break
        window = chosen(days)
        output["calendar"] = {day.isoformat(): value for day, value in sorted(days.items()) if day <= target}
        if window:
            output.update(status="verified", verified=True, calendar_verified=True, resolved_trade_date=target.isoformat(),
                          trading_dates=[day.isoformat() for day in window], window_start=window[0].isoformat(), window_end=target.isoformat())
        elif days.get(target) is False:
            output["status"] = "non_trading_day"
        else:
            output["validation_error"] = "可信日历范围不足，无法确认所需交易日窗口；没有使用工作日推测"
        output["source_metadata"] = [{"path": path, "raw_hash": response["raw_hash"], "fetched_at": response["fetched_at"], "parameters": response["parameters"]}
                                     for _, path, response, packet_days in packets if any(day in packet_days for day in (window or [target]))]
    except (ValueError, KeyError, TypeError, OSError) as exc:
        output["validation_error"] = str(exc)
    output["completed_at"] = datetime.now(SHANGHAI).isoformat()
    return output


def session_worker() -> int:
    import baostock as sdk
    protocol = sys.stdout
    session: dict[str, Any] = {}
    for line in sys.stdin:
        request = json.loads(line)
        if set(request) != {"request_id", "operation", "parameters"} or type(request["request_id"]) is not int or request["operation"] not in SESSION_OPERATIONS:
            return 2

        def emit(event):
            protocol.write(json.dumps({**event, "request_id": request["request_id"]}, ensure_ascii=False) + "\n")
            protocol.flush()

        buffer = io.StringIO()
        log = BoundedSDKLog(buffer)
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = execute_request(request, sdk=sdk, emit=emit, diagnostic_stages=True, session_state=session)
        log.flush()
        result["sdk_log"] = clean_log(buffer.getvalue())
        emit({"event": "final", "result": result})
        if not result["ok"]:
            return 0  # Parent reconnects only within its finite retry budget.
    if session.get("login") is not None:
        with contextlib.redirect_stdout(sys.stderr):
            sdk.logout()
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--session-worker"]:
        raise SystemExit("module only supports --session-worker")
    raise SystemExit(session_worker())
