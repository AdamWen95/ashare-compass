"""Bounded serial HTTPS transport for explicitly permitted website providers.

A persistent child owns TLS connections. Parent deadlines include DNS, connect,
headers and body reads; a stuck request is killed, not left in a background
thread. No redirects, proxy/host rotation, browser impersonation or credentials.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime
import hashlib
import http.client
import json
import os
import queue
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo


HTTP_SCHEMA = "bounded-http-response-v1"
MAX_BODY_BYTES = 2_000_000
USER_AGENT = "ashare-daily-research/0.5 (personal noncommercial local research; bounded client)"
ENDPOINTS = frozenset({"https://push2his.eastmoney.com/api/qt/stock/kline/get", "https://push2.eastmoney.com/api/qt/stock/get"})
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _request(endpoint: str, parameters: dict) -> dict:
    if endpoint not in ENDPOINTS or not isinstance(parameters, dict) or not parameters:
        raise ValueError("explicit allowed HTTPS endpoint and parameters required")
    if any(not isinstance(key, str) or not isinstance(value, str) or len(key) > 50 or len(value) > 500
           for key, value in parameters.items()) or len(urlencode(parameters)) > 4000:
        raise ValueError("invalid bounded HTTP parameters")
    return {"url": endpoint, "parameters": dict(parameters)}


def _empty(request: dict) -> dict:
    return {"schema_version": HTTP_SCHEMA, "request": request, "ok": False, "status": "not_requested",
            "error_code": "not_requested", "error_msg": "", "http_status": None, "headers": {},
            "body_base64": "", "body_sha256": hashlib.sha256(b"").hexdigest(), "body_complete": False,
            "fetched_at": datetime.now(SHANGHAI).isoformat(), "provenance_mode": "online", "verification_kind": "live_network"}


class HttpClient:
    """One serial worker/session; permission/rate refusals latch until new client."""
    def __init__(self, *, timeout_seconds: float = 15, max_attempts: int = 2, pause_seconds: float = .5):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not .01 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be positive and at most 60")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 2:
            raise ValueError("max_attempts must be one or two")
        if isinstance(pause_seconds, bool) or not isinstance(pause_seconds, (int, float)) or not 0 <= pause_seconds <= 5:
            raise ValueError("pause_seconds must be between zero and five")
        self.timeout_seconds, self.max_attempts, self.pause_seconds = timeout_seconds, max_attempts, pause_seconds
        self._process, self._events = None, None
        self._busy = threading.Lock()
        self._request_id, self._last_request_at, self._stopped = 0, None, None

    def get(self, endpoint: str, parameters: dict) -> dict:
        request = _request(endpoint, parameters)
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("HTTP source concurrency is prohibited")
        try:
            if self._stopped:
                result = _empty(request)
                result.update(status="circuit_open", error_code="source_stopped", error_msg="Previous HTTP permission/rate refusal; no request sent.")
                result["metrics"] = {"requests": 0, "retries": 0, "elapsed_seconds": 0, "attempts": [], "network_sent": False}
                return result
            started, attempts, evidence = time.monotonic(), [], []
            for index in range(self.max_attempts):
                if self._last_request_at is not None:
                    wait = self.pause_seconds - (time.monotonic() - self._last_request_at)
                    if wait > 0:
                        time.sleep(wait)
                self._last_request_at = time.monotonic()
                result = self._attempt(request)
                evidence.append(deepcopy(result))
                attempts.append({"attempt": index + 1, "status": result["status"], "error_code": result["error_code"],
                                 "http_status": result["http_status"], "elapsed_seconds": result.get("elapsed_seconds"),
                                 "failure_stage": result.get("failure_stage"), "stages": result.get("stages", [])})
                if result["status"] in {"permission_denied", "rate_limited"}:
                    self._stopped = result["status"]
                    self.close()
                    break
                retryable = result["status"] in {"network_error", "timeout", "server_error"}
                # Do not retry earlier than an upstream Retry-After. A future
                # invocation may be scheduled by its caller; no unbounded wait.
                if not retryable or result.get("headers", {}).get("retry-after"):
                    break
            result["attempt_evidence"] = evidence
            result["metrics"] = {"requests": len(attempts), "retries": max(0, len(attempts) - 1),
                                 "elapsed_seconds": round(time.monotonic() - started, 6), "attempts": attempts,
                                 "stages": result.get("stages", []), "failure_stage": result.get("failure_stage"),
                                 "network_sent": bool(attempts), "hard_timeout_seconds": self.timeout_seconds,
                                 "body_bytes": len(base64.b64decode(result["body_base64"])),
                                 "status": result["status"], "request_url": endpoint,
                                 "mode": "research", "verification_kind": "live_network"}
            return result
        finally:
            self._busy.release()

    def _start(self):
        self.close()
        options = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
                   "text": True, "encoding": "utf-8", "errors": "strict", "bufsize": 1}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "ashare_daily.providers.http", "--worker"], **options)
        self._events = queue.Queue()
        process, events = self._process, self._events

        def reader():
            try:
                while line := process.stdout.readline(4_000_001):
                    if len(line) > 4_000_000:
                        events.put({"protocol_error": "worker result exceeded body bound"})
                        return
                    events.put(line)
            except (OSError, ValueError, UnicodeError):
                pass
            finally:
                events.put(None)

        threading.Thread(target=reader, daemon=True).start()

    def _attempt(self, request: dict) -> dict:
        result, started, stages = _empty(request), time.monotonic(), []
        active_stage = "worker_start"
        try:
            if self._process is None or self._process.poll() is not None:
                self._start()
            self._request_id += 1
            request_id = self._request_id
            self._process.stdin.write(json.dumps({"request_id": request_id, "request": request, "timeout_seconds": self.timeout_seconds}) + "\n")
            self._process.stdin.flush()
            while True:
                remaining = self.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise queue.Empty
                event = self._events.get(timeout=remaining)
                if event is None:
                    raise ConnectionError("HTTP worker exited before a complete response")
                if isinstance(event, dict):
                    raise ValueError(event.get("protocol_error", "worker protocol error"))
                event = json.loads(event)
                if event.get("request_id") != request_id:
                    raise ValueError("HTTP worker request identity mismatch")
                if event.get("event") == "stage":
                    stages.append(event)
                    active_stage = event["stage"]
                elif event.get("event") == "final":
                    candidate = event.get("result")
                    if not isinstance(candidate, dict) or candidate.get("schema_version") != HTTP_SCHEMA or candidate.get("request") != request:
                        raise ValueError("HTTP worker response schema mismatch")
                    result = candidate
                    active_stage = None if result["ok"] else active_stage
                    break
                else:
                    raise ValueError("unexpected HTTP worker event")
        except queue.Empty:
            result.update(status="timeout", error_code="hard_timeout", error_msg="HTTP worker exceeded the total request deadline and was terminated.")
            self.close()
        except (OSError, ConnectionError) as exc:
            result.update(status="network_error", error_code="network_exception", error_msg=type(exc).__name__ + ": " + str(exc)[:300])
            self.close()
        except (ValueError, TypeError, KeyError) as exc:
            result.update(status="schema_changed", error_code="worker_protocol_error", error_msg=str(exc)[:300])
            self.close()
        result.update(stages=stages, failure_stage=active_stage,
                      elapsed_seconds=round(time.monotonic() - started, 6), fetched_at=datetime.now(SHANGHAI).isoformat())
        return result

    def close(self):
        process, self._process = self._process, None
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass


def _request_in_worker(request: dict, timeout_seconds: float, connections: dict, emit) -> dict:
    request = _request(request["url"], request["parameters"])
    result, raw, started = _empty(request), b"", time.monotonic()
    url = urlsplit(request["url"])
    connection = connections.get(url.hostname)
    try:
        emit("connect")
        if connection is None:
            connection = http.client.HTTPSConnection(url.hostname, timeout=min(5, timeout_seconds), context=ssl.create_default_context())
            connections[url.hostname] = connection
        if connection.sock is None:
            connection.connect()
        connection.sock.settimeout(max(.001, timeout_seconds - (time.monotonic() - started)))
        emit("request_headers")
        connection.request("GET", url.path + "?" + urlencode(request["parameters"]),
                           headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "identity"})
        emit("response_headers")
        response = connection.getresponse()
        result["http_status"] = response.status
        # Do not retain cookies/auth headers in the evidence log.
        result["headers"] = {key.lower(): value for key, value in response.getheaders()
                             if key.lower() in {"content-type", "content-length", "content-encoding", "date", "retry-after", "location"}}
        if response.status in {401, 403, 429}:
            result.update(status="permission_denied" if response.status in {401, 403} else "rate_limited",
                          error_code="http_" + str(response.status), error_msg="HTTP source access refusal; no further body/request read.")
            return result
        emit("read_body")
        while len(raw) <= MAX_BODY_BYTES:
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("HTTP body read exceeded deadline")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, MAX_BODY_BYTES + 1 - len(raw)))
            if not chunk:
                result["body_complete"] = True
                break
            raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            raw = raw[:MAX_BODY_BYTES]
            result.update(status="schema_changed", error_code="body_too_large", error_msg="HTTP body exceeds two MB; source response is incomplete.")
        elif result["headers"].get("content-encoding", "identity").lower() not in {"", "identity"}:
            result.update(status="schema_changed", error_code="unsupported_content_encoding", error_msg="Expected uncompressed bounded JSON body.")
        elif response.status == 200:
            length = result["headers"].get("content-length")
            if length is not None and (not length.isdigit() or int(length) != len(raw)):
                result.update(status="schema_changed", error_code="body_length_mismatch", error_msg="HTTP declared body length differs from received bytes.", body_complete=False)
            else:
                result.update(ok=True, status="received", error_code="0", error_msg="")
        else:
            status = "redirect_refused" if 300 <= response.status < 400 else "server_error" if response.status >= 500 else "http_error"
            result.update(status=status, error_code="http_" + str(response.status), error_msg="HTTP response did not return 200; redirects are not followed.")
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        category = "timeout" if isinstance(exc, TimeoutError) else "tls_error" if isinstance(exc, ssl.SSLError) else "network_error"
        result.update(status=category, error_code=category, error_msg=type(exc).__name__ + ": " + str(exc)[:300], body_complete=False)
    finally:
        result.update(body_base64=base64.b64encode(raw).decode("ascii"), body_sha256=hashlib.sha256(raw).hexdigest(),
                      fetched_at=datetime.now(SHANGHAI).isoformat())
        if not result["ok"]:
            connections.pop(url.hostname, None)
            if connection is not None:
                connection.close()
    return result


def worker() -> int:
    connections = {}
    try:
        for line in sys.stdin:
            packet = json.loads(line)
            if set(packet) != {"request_id", "request", "timeout_seconds"} or type(packet["request_id"]) is not int:
                return 2

            def emit(stage):
                print(json.dumps({"request_id": packet["request_id"], "event": "stage", "stage": stage}), flush=True)

            result = _request_in_worker(packet["request"], packet["timeout_seconds"], connections, emit)
            print(json.dumps({"request_id": packet["request_id"], "event": "final", "result": result}, ensure_ascii=False), flush=True)
    finally:
        for connection in connections.values():
            connection.close()
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("module only supports --worker")
    raise SystemExit(worker())
