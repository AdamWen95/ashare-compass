"""每次启动只执行 login → 单次只读查询 → logout，stdout 为 JSON 行协议。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from datetime import datetime
from typing import Any, Callable

from .baostock import (
    DISCOVERY_OPERATIONS, MAX_LOG_CHARS, MAX_ROWS, SHANGHAI, BaoStockAdjustmentFlag, base_result, classify_error, clean_log,
    expected_fields, raw_hash, validate_request,
)


class SchemaError(ValueError):
    pass


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class BoundedSDKLog(io.TextIOBase):
    """按行脱敏后写 stderr，保留阻塞前日志且约束总长度。"""

    def __init__(self, target: Any):
        self.target, self.pending, self.written = target, "", 0

    def write(self, value: str) -> int:
        if self.written < MAX_LOG_CHARS:
            self.pending = (self.pending + value)[:MAX_LOG_CHARS]
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                self._send(line + "\n")
        return len(value)

    def _send(self, value: str) -> None:
        text = clean_log(value, MAX_LOG_CHARS - self.written)
        self.target.write(text)
        self.target.flush()
        self.written += len(text)

    def flush(self) -> None:
        if self.pending:
            self._send(self.pending)
            self.pending = ""


def check_result(result: Any) -> tuple[str, str]:
    if result is None or not isinstance(getattr(result, "error_code", None), str) or not isinstance(getattr(result, "error_msg", None), str):
        raise SchemaError("SDK 返回 None 或缺少字符串 error_code/error_msg")
    code, message = result.error_code, clean_log(result.error_msg, 1024)
    if code != "0":
        raise ProviderError(code, message)
    return code, message


def read_rows(result: Any, wanted_fields: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    check_result(result)
    fields = getattr(result, "fields", None)
    if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields) or len(set(fields)) != len(fields) or set(fields) != set(wanted_fields):
        raise SchemaError("返回字段缺失、重复、增多或不符合本次只读查询")
    rows: list[dict[str, str]] = []
    while True:
        check_result(result)
        has_next = result.next()
        check_result(result)  # SDK 的 next() 可能更新错误码后返回 False。
        if not isinstance(has_next, bool):
            raise SchemaError("SDK next() 没有返回布尔值")
        if not has_next:
            break
        if len(rows) >= MAX_ROWS:
            raise SchemaError(f"返回超过 {MAX_ROWS} 行，停止小样本请求")
        row = result.get_row_data()
        check_result(result)
        if not isinstance(row, list) or len(row) != len(fields) or any(not isinstance(value, str) or len(value) > 4096 for value in row):
            raise SchemaError("返回行列数不符或含非字符串/过长字段；不截断、不补零")
        rows.append(dict(zip(fields, row, strict=True)))
    return fields, rows


def read_discovery_rows(result: Any, wanted_fields: list[str], pagination: dict[str, Any], *, observe: Callable[..., None] | None = None) -> tuple[list[str], list[dict[str, str]]]:
    """Consume all SDK pages and verify a short/empty terminal page.

    BaoStock 0.9.3 next() may return False on a lost page response without
    changing error_code. A full current page is therefore never EOF evidence.
    """
    from baostock.common.contants import BAOSTOCK_PER_PAGE_COUNT

    observe = observe or (lambda *args, **kwargs: None)
    check_result(result)
    fields = getattr(result, "fields", None)
    if not isinstance(fields, list) or len(set(fields)) != len(fields) or set(fields) != set(wanted_fields):
        raise SchemaError("完整名单字段缺失、重复或变化")
    page_size = BAOSTOCK_PER_PAGE_COUNT
    pagination.update(page_size=page_size, pages=[], observed_records=0, exhausted=False)
    rows: list[dict[str, str]] = []
    previous_page = 0
    while True:
        check_result(result)
        try:
            page = int(result.cur_page_num)
            server_size = int(result.per_page_count)
            data, cursor = result.data, result.cur_row_num
        except (AttributeError, TypeError, ValueError) as exc:
            raise SchemaError("SDK 分页元数据无效，不能确认完整名单") from exc
        if server_size != page_size or not isinstance(data, list) or type(cursor) is not int:
            raise SchemaError("SDK 分页大小或游标不符合已核验契约")
        if page != previous_page:
            if page != previous_page + 1 or cursor != 0 or len(data) > page_size:
                raise SchemaError("SDK 页码不连续、页游标非零或页大小异常")
            pagination["pages"].append({"page": page, "records": len(data)})
            observe("page_observed", "observed", page=page, records=len(data), observed_records=len(rows))
            previous_page = page
        if cursor < 0 or cursor > len(data):
            raise SchemaError("SDK 行游标越界")
        requesting_page = cursor == len(data) and len(data) == page_size
        if requesting_page:
            observe("page_wait", "started", requested_page=page + 1, observed_records=len(rows))
        has_next = result.next()
        if requesting_page:
            observe("page_wait", "completed", requested_page=page + 1,
                    returned_page=getattr(result, "cur_page_num", None), error_code=getattr(result, "error_code", None))
        check_result(result)
        if type(has_next) is not bool:
            raise SchemaError("SDK next() 没有返回布尔值")
        # next() can load a new page; inspect it before consuming its first row.
        if int(result.cur_page_num) != page:
            if int(result.cur_page_num) != page + 1 or cursor != len(data) or len(data) != page_size:
                raise SchemaError("SDK 提前翻页或跳页，完整名单被截断")
            if has_next:
                continue
            # A legitimate empty final page is also part of the page ledger.
            if result.data != [] or result.cur_row_num != 0:
                raise SchemaError("SDK 末页状态矛盾")
            pagination["pages"].append({"page": page + 1, "records": 0})
            observe("page_observed", "observed", page=page + 1, records=0, observed_records=len(rows))
        if not has_next:
            observe("terminal_validation", "started", page=int(result.cur_page_num), records=len(result.data), observed_records=len(rows))
            if result.cur_row_num != len(result.data) or len(result.data) >= page_size:
                raise SchemaError("SDK 满页后未返回已确认末页；可能网络空响应或分页截断")
            pagination.update(exhausted=True, terminal_reason="short_or_empty_page", observed_records=len(rows))
            observe("terminal_validation", "completed", terminal_reason="short_or_empty_page", observed_records=len(rows))
            break
        before = result.cur_row_num
        row = result.get_row_data()
        check_result(result)
        if result.cur_row_num != before + 1 or not isinstance(row, list) or len(row) != len(fields) or any(not isinstance(value, str) or len(value) > 4096 for value in row):
            raise SchemaError("SDK 名单行或游标异常；不截断、不补零")
        rows.append(dict(zip(fields, row, strict=True)))
        pagination["observed_records"] = len(rows)
    return fields, rows


def execute_request(request: dict[str, Any], *, sdk: Any = None, emit: Callable[[dict[str, Any]], None] | None = None, diagnostic_stages: bool = False, session_state: dict[str, Any] | None = None) -> dict[str, Any]:
    operation = request["operation"]
    parameters = validate_request(operation, request["parameters"])
    result = base_result(operation, parameters)
    started = time.monotonic()
    emit = emit or (lambda event: None)
    stage_starts: dict[str, float] = {}

    def observe(stage: str, state: str, **details: Any) -> None:
        if not diagnostic_stages:
            return
        current = time.monotonic()
        event = {"event": "stage", "stage": stage, "state": state,
                 "observed_at": datetime.now(SHANGHAI).isoformat(),
                 "elapsed_seconds": round(current - started, 6), **details}
        if state == "started":
            stage_starts[stage] = current
        elif state == "completed" and stage in stage_starts:
            event["stage_seconds"] = round(current - stage_starts.pop(stage), 6)
        emit(event)

    logged_in = False
    if sdk is None:
        import baostock as sdk
    try:
        reused_login = session_state is not None and session_state.get("login") is not None
        if reused_login:
            login = session_state["login"]
            observe("login_reused", "observed", first_login_at=session_state["login_at"])
        else:
            observe("login_wait", "started")
            login = sdk.login()  # 官方 SDK 默认匿名只读登录，不传账号或 API key。
        if login is not None and isinstance(getattr(login, "error_code", None), str) and isinstance(getattr(login, "error_msg", None), str):
            result["login"] = {"ok": login.error_code == "0", "error_code": login.error_code, "error_msg": clean_log(login.error_msg, 1024)}
        emit({"event": "login", "login": result["login"]})
        if not reused_login:
            observe("login_wait", "completed", error_code=result["login"]["error_code"])
        check_result(login)
        if session_state is not None:
            if not reused_login:
                session_state.update(login=login, login_at=datetime.now(SHANGHAI).isoformat(), requests=0)
            session_state["requests"] += 1
            result["session"] = {"login_reused": reused_login, "login_at": session_state["login_at"],
                                 "request_index": session_state["requests"], "logout_deferred": True}
        logged_in = True
        fields = expected_fields(operation, parameters)
        observe("query_wait", "started", operation=operation, parameters=parameters)
        if operation == "calendar":
            response = sdk.query_trade_dates(start_date=parameters["start_date"], end_date=parameters["end_date"])
        elif operation in {"basic", "basic_all"}:
            response = sdk.query_stock_basic(code=parameters.get("code", ""))
        elif operation == "universe":
            response = sdk.query_all_stock(day=parameters["day"])
        else:
            response = sdk.query_history_k_data_plus(
                parameters["code"], ",".join(fields), start_date=parameters["start_date"],
                end_date=parameters["end_date"], frequency="d",
                adjustflag=(BaoStockAdjustmentFlag.FORWARD_ADJUSTED.value
                            if parameters["adjustment_mode"] == "forward_adjusted"
                            else BaoStockAdjustmentFlag.UNADJUSTED.value),
            )
        observe("query_wait", "completed", error_code=getattr(response, "error_code", None),
                initial_page=getattr(response, "cur_page_num", None),
                initial_page_records=len(response.data) if isinstance(getattr(response, "data", None), list) else None)
        # A query response carrying an SDK error has not entered the row
        # reader. Preserve that boundary before declaring row_read started.
        check_result(response)
        observe("row_read", "started")
        if operation in DISCOVERY_OPERATIONS:
            result["pagination"] = {}
            actual_fields, rows = read_discovery_rows(response, fields, result["pagination"], observe=observe)
        else:
            actual_fields, rows = read_rows(response, fields)
        observe("row_read", "completed", row_count=len(rows))
        result.update(ok=True, status="ok" if rows else "empty_confirmed", error_code="0", error_msg=clean_log(response.error_msg, 1024), fields=actual_fields, rows=rows, raw_hash=raw_hash(actual_fields, rows), fetched_at=datetime.now(SHANGHAI).isoformat())
    except ProviderError as exc:
        result.update(status=classify_error(exc.code, exc.message), error_code=exc.code, error_msg=exc.message)
    except (TimeoutError, ConnectionError, OSError) as exc:
        result.update(status="timeout" if isinstance(exc, TimeoutError) else "unknown", error_code="worker_timeout" if isinstance(exc, TimeoutError) else "network_exception", error_msg=clean_log(f"{type(exc).__name__}: {exc}", 1024))
    except (SchemaError, AttributeError, TypeError, ValueError, IndexError, KeyError) as exc:
        result.update(status="schema_changed", error_code="schema_changed", error_msg=clean_log(f"{type(exc).__name__}: {exc}", 1024))
    except Exception as exc:
        result.update(status="unknown", error_code="sdk_exception", error_msg=clean_log(f"{type(exc).__name__}: {exc}", 1024))
    finally:
        emit({"event": "query", "ok": result["ok"], "status": result["status"], "error_code": result["error_code"], "row_count": len(result["rows"])})
        if logged_in and session_state is None:
            try:
                observe("logout_wait", "started")
                logout = sdk.logout()
                observe("logout_wait", "completed", error_code=getattr(logout, "error_code", None))
                check_result(logout)
                result["logout"] = {"ok": True, "error_code": "0", "error_msg": clean_log(logout.error_msg, 1024)}
            except ProviderError as exc:
                result["logout"] = {"ok": False, "error_code": exc.code, "error_msg": exc.message}
                if classify_error(exc.code, exc.message) in {"permission_denied", "rate_limited"}:
                    # 任一阶段明确拒绝权限或限流，都交给外层停止该源。
                    result.update(ok=False, status=classify_error(exc.code, exc.message), error_code=exc.code, error_msg=exc.message, fields=[], rows=[], raw_hash=raw_hash([], []))
            except Exception as exc:
                result["logout"] = {"ok": False, "error_code": "logout_exception", "error_msg": clean_log(f"{type(exc).__name__}: {exc}", 1024)}
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return result


def main() -> int:
    protocol_stdout = sys.stdout

    def emit(event: dict[str, Any]) -> None:
        protocol_stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        protocol_stdout.flush()

    sdk_log = BoundedSDKLog(sys.stderr)
    try:
        request = json.loads(sys.stdin.read(8192))
        if (not isinstance(request, dict) or not {"operation", "parameters"} <= set(request)
                or set(request) - {"operation", "parameters", "diagnostic_stages"}
                or type(request.get("diagnostic_stages", False)) is not bool):
            raise ValueError("worker 请求只允许 operation、parameters 和可选布尔 diagnostic_stages")
        with contextlib.redirect_stdout(sdk_log), contextlib.redirect_stderr(sdk_log):
            result = execute_request(request, emit=emit, diagnostic_stages=request.get("diagnostic_stages", False))
        sdk_log.flush()
        emit({"event": "final", "result": result})
        return 0
    except Exception as exc:
        sdk_log.write(clean_log(f"{type(exc).__name__}: {exc}") + "\n")
        sdk_log.flush()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
