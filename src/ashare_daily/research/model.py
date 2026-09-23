"""A bounded, replaceable OpenAI-compatible Chat Completions adapter.

It has no tools, file readers, database writers, or model-chosen network routes.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, ValidationError

from .model_settings import ModelSettings, load_model_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward credentials, even to an unexpected same-host route.
        return None


def http_transport(url: str, headers: dict, payload: dict, timeout: float) -> HTTPResponse:
    request = Request(url, data=_canonical(payload).encode("utf-8"), headers=headers, method="POST")
    opener = build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("model_response_size_limit")
        return HTTPResponse(response.code, body, {key.lower(): value for key, value in response.headers.items()})


def _redact(value: object, secret: str) -> object:
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[^\s\"'<>,;]+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{6,}", "[REDACTED]", value)
        return value
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if re.search(r"(?i)api.?key|authorization|secret|access.?token", str(k))
                         else _redact(v, secret)) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_redact(v, secret) for v in value]
    return value


def _usage(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    # Preserve provider-reported counters; no estimates or fabricated currency.
    result = {key: amount for key, amount in value.items()
              if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
              and isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0}
    for name in ("prompt_tokens_details", "completion_tokens_details"):
        if isinstance(value.get(name), dict):
            result[name] = {key: amount for key, amount in value[name].items()
                            if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0}
    return result or None


class ChatCompletionsModel:
    def __init__(self, settings: ModelSettings, *, transport: Callable | None = None,
                 sleep: Callable = time.sleep, attempt_guard=None):
        self.settings = settings
        self.transport = transport or http_transport
        self.verification_kind = "offline_test" if transport is not None else "real_network"
        self.sleep = sleep
        self.call_count = 0
        self.records: list[dict] = []
        self.stopped_reason: str | None = None
        self.attempt_guard = attempt_guard

    def _result(self, status: str, start_count: int, attempts: list[dict], **values) -> dict:
        result = {"status": status, "content": None, "http_status": None, "usage": None,
                  "finish_reason": None, "call_count": self.call_count - start_count,
                  "total_call_count": self.call_count, "retries": max(0, len(attempts) - 1),
                  "attempts": attempts, "verification_kind": self.verification_kind, **values}
        return _redact(result, self.settings.api_key.get_secret_value())

    def complete(self, messages: list[dict], *, response_mode: str = "text", schema: dict | None = None,
                 max_output_tokens: int | None = None) -> dict:
        start_count = self.call_count
        attempts: list[dict] = []
        if self.settings.missing_fields:
            return self._result("missing_configuration", start_count, attempts,
                                missing_fields=self.settings.missing_fields)
        if self.stopped_reason:
            return self._result("stopped", start_count, attempts, reason=self.stopped_reason)
        if (response_mode not in {"text", "json_object", "json_schema"}
                or (response_mode == "json_schema" and not isinstance(schema, dict))):
            return self._result("invalid_request", start_count, attempts, reason="响应格式或 schema 无效")
        if (not isinstance(messages, list) or not messages or len(messages) > 12
                or any(not isinstance(item, dict) or set(item) != {"role", "content"}
                       or item["role"] not in {"system", "user", "assistant"}
                       or not isinstance(item["content"], str) for item in messages)):
            return self._result("invalid_request", start_count, attempts, reason="只接受有限的纯文本 messages")
        if sum(len(item["content"]) for item in messages) > self.settings.max_input_chars:
            return self._result("input_limit", start_count, attempts, reason="输入超过配置上限，未静默截断")
        if any(self.settings.api_key.get_secret_value() in item["content"] for item in messages):
            return self._result("invalid_request", start_count, attempts, reason="输入含本地凭据，未发送")
        output_limit = max_output_tokens if max_output_tokens is not None else self.settings.max_output_tokens
        if not isinstance(output_limit, int) or not 1 <= output_limit <= self.settings.max_output_tokens:
            return self._result("invalid_request", start_count, attempts, reason="输出上限无效")
        payload = {"model": self.settings.model_name, "messages": messages,
                   self.settings.output_token_parameter: output_limit}
        if response_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif response_mode == "json_schema":
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "ashare_research", "strict": True, "schema": schema}}
        request_hash = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        secret = self.settings.api_key.get_secret_value()
        headers = {"Authorization": "Bearer " + secret, "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": "ashare-daily-research/M3"}
        last_status, last_values = "unknown", {}
        for number in range(self.settings.max_retries + 1):
            if self.call_count >= self.settings.max_calls:
                return self._result("budget_exhausted", start_count, attempts,
                                    reason="本次运行网络调用预算已耗尽", previous_status=last_status)
            reservation = None
            if self.attempt_guard is not None:
                try:
                    reservation = self.attempt_guard.before_attempt()
                except Exception:
                    self.stopped_reason = "daily_budget_unavailable_or_exhausted"
                    return self._result("budget_exhausted", start_count, attempts,
                                        reason="每日累计预算不可用或已耗尽，未发起新的请求")
            self.call_count += 1
            began = time.monotonic()
            record = {"call_number": self.call_count, "fetched_at": _now(), "endpoint_url": self.settings.endpoint_url,
                      "model_name": self.settings.model_name, "response_mode": response_mode,
                      "request_hash": request_hash, "request_fields": sorted(payload),
                      "attempt_number": number + 1, "verification_kind": self.verification_kind,
                      "http_status": None, "usage": None}
            retry_after = None
            try:
                response = self.transport(self.settings.endpoint_url, headers, payload, self.settings.timeout_seconds)
                if not isinstance(response, HTTPResponse):
                    raise ValueError("invalid_transport_response")
                record["http_status"] = response.status
                body_text = response.body.decode("utf-8", errors="replace")
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                if 300 <= response.status < 400:
                    last_status, last_values = "redirect_blocked", {"reason": "未跟随重定向或转发凭据"}
                elif response.status != 200:
                    last_status = ({401: "authentication_failed", 403: "permission_denied", 429: "rate_limited",
                                    408: "timeout"}.get(response.status)
                                   or ("server_error" if response.status >= 500 else "request_rejected"))
                    last_values = {"error_excerpt": str(_redact(body_text, secret))[:500]}
                    if response.status in {401, 403}:
                        self.stopped_reason = last_status
                    retry_after = response_headers.get("retry-after")
                else:
                    try:
                        body = json.loads(body_text)
                        if not isinstance(body, dict):
                            raise ValueError("not_object")
                        choices = body.get("choices")
                        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                            raise ValueError("invalid_choices")
                        choice = choices[0]
                        message = choice.get("message")
                        if not isinstance(message, dict):
                            raise ValueError("invalid_message")
                        finish_reason = choice.get("finish_reason")
                        content = message.get("content")
                        record["usage"] = _usage(body.get("usage"))
                        if message.get("refusal") or finish_reason == "content_filter":
                            last_status = "refused"
                        elif finish_reason == "length":
                            last_status = "truncated"
                        elif message.get("tool_calls") or message.get("function_call"):
                            last_status = "unexpected_tool_call"
                        elif not isinstance(content, str) or not content.strip():
                            last_status = "empty_response"
                        elif finish_reason != "stop":
                            last_status = "incomplete_response"
                        else:
                            last_status = "ok"
                        last_values = {"content": content if isinstance(content, str) else None,
                                       "finish_reason": finish_reason, "usage": record["usage"],
                                       "response_model": body.get("model") if isinstance(body.get("model"), str) else None,
                                       "response": {"id": body.get("id"), "model": body.get("model"),
                                                    "choices": choices, "usage": record["usage"]}}
                    except (ValueError, TypeError, KeyError):
                        last_status, last_values = "invalid_response", {"reason": "HTTP 200 但非有效 Chat Completions 正文"}
            except (socket.timeout, TimeoutError):
                last_status, last_values = "timeout", {"reason": "请求超时，异常细节不记录以保护凭据"}
            except URLError as exc:
                last_status = "timeout" if isinstance(exc.reason, (socket.timeout, TimeoutError)) else "network_error"
                last_values = {"reason": "网络请求未成功；未记录可能含凭据的底层异常"}
            except Exception:
                last_status, last_values = "transport_error", {"reason": "传输失败，未记录可能含凭据的底层异常"}
            record.update(status=last_status, elapsed_seconds=round(time.monotonic() - began, 4),
                          **last_values)
            record = _redact(record, secret)
            attempts.append(record)
            self.records.append(record)
            if self.attempt_guard is not None:
                try:
                    self.attempt_guard.after_attempt(reservation, record)
                except Exception:
                    # The reservation remains spent even if usage bookkeeping fails.
                    self.stopped_reason = "budget_record_failed"
                    return self._result("budget_record_failed", start_count, attempts,
                                        reason="请求计次已保留；用量登记失败，停止新增调用")
            if last_status not in {"timeout", "network_error", "server_error", "rate_limited"}:
                break
            if number >= self.settings.max_retries or self.call_count >= self.settings.max_calls:
                break
            delay = min(2 ** number, 4)
            if last_status == "rate_limited":
                # Honor a short explicit wait. Missing/long/date-form waits stop this source.
                try:
                    wait = float(retry_after)
                except (ValueError, TypeError):
                    wait = -1
                if not 0 <= wait <= 5:
                    self.stopped_reason = "rate_limited"
                    break
                delay = max(delay, wait)
            self.sleep(delay)
        if last_status == "rate_limited":
            self.stopped_reason = "rate_limited"
        return self._result(last_status, start_count, attempts,
                            http_status=attempts[-1]["http_status"] if attempts else None, **last_values)

    def summary(self) -> dict:
        totals = {}
        reported = 0
        for record in self.records:
            if record.get("usage"):
                reported += 1
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if key in record["usage"]:
                        totals[key] = totals.get(key, 0) + record["usage"][key]
        return {"call_count": self.call_count, "retry_count": sum(r["attempt_number"] > 1 for r in self.records),
                "max_calls": self.settings.max_calls, "provider_reported_usage": totals or None,
                "calls_with_usage": reported, "calls_without_usage": self.call_count - reported,
                "currency_cost": None, "cost_status": "价格未配置，不估算金额",
                "stopped_reason": self.stopped_reason, "verification_kind": self.verification_kind}


def parse_json_object(content: str) -> dict:
    """Accept plain JSON only; fenced prose is not quietly treated as valid JSON."""
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("模型 JSON 不接受重复键")
            result[key] = value
        return result
    result = json.loads(content, object_pairs_hook=unique_keys,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    if not isinstance(result, dict):
        raise ValueError("模型 JSON 必须为对象")
    return result


def complete_validated(client: ChatCompletionsModel, messages: list[dict], output_model: type[BaseModel],
                       *, response_mode: str = "text") -> dict:
    """Validate structure and allow at most one format repair; not evidence correctness."""
    schema = output_model.model_json_schema()
    results = []
    for repair in range(2):
        result = client.complete(messages, response_mode=response_mode,
                                 schema=schema if response_mode == "json_schema" else None)
        results.append(result)
        if result["status"] != "ok":
            return {"status": result["status"], "parsed": None, "format_repairs": repair, "responses": results}
        try:
            parsed = output_model.model_validate(parse_json_object(result["content"]), strict=True)
            return {"status": "ok", "parsed": parsed.model_dump(mode="json"),
                    "format_repairs": repair, "responses": results}
        except (ValueError, ValidationError):
            if repair == 0:
                messages = [*messages, {"role": "assistant", "content": result["content"]},
                            {"role": "user", "content": "上次输出不是符合约定结构的 JSON 对象。仅修复格式；不能新增事实、证据或数值。仅返回满足此 schema 的 JSON：" + _canonical(schema)}]
    return {"status": "invalid_json", "parsed": None, "format_repairs": 1, "responses": results}


class _ProbeJSON(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ok: Literal[True]


def doctor_model(output_dir: Path | str = Path("outputs/research/m3_model"),
                 env_file: Path | str = Path(".env"), *, settings: ModelSettings | None = None,
                 client: ChatCompletionsModel | None = None) -> dict:
    settings = settings or (client.settings if client is not None else load_model_settings(env_file))
    client = client or ChatCompletionsModel(settings)
    directory = Path(output_dir).resolve() / (datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f") + "-doctor-model-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    probes = [
        ("basic_text", "text", "请只回复一句短文本：连接检查。", None),
        ("plain_json", "text", '仅回复这个 JSON 对象：{"ok":true}', None),
        ("json_mode", "json_object", '仅回复这个 JSON 对象：{"ok":true}', None),
        ("strict_schema", "json_schema", '仅回复符合 schema 的 JSON 对象：{"ok":true}', _ProbeJSON.model_json_schema()),
    ]
    capabilities = {}
    basic_ok = False
    for name, mode, content, schema in probes:
        if name != "basic_text" and not basic_ok:
            capabilities[name] = {"status": "not_tested", "reason": "基础请求未成功", "call_count": 0}
            continue
        response = client.complete([{"role": "user", "content": content}], response_mode=mode,
                                   schema=schema, max_output_tokens=min(64, settings.max_output_tokens))
        capability = {"status": response["status"], "response": response, "valid_json": None,
                      "schema_valid": None, "call_count": response["call_count"]}
        if name == "basic_text":
            basic_ok = response["status"] == "ok"
        elif response["status"] == "ok":
            capability["valid_json"] = False
            try:
                parsed = parse_json_object(response["content"])
                capability["valid_json"] = True
                _ProbeJSON.model_validate(parsed, strict=True)
                capability["schema_valid"] = True
            except (ValueError, ValidationError):
                capability["status"] = "invalid_json_or_schema"
                capability["schema_valid"] = False
        capabilities[name] = capability
    preferred = "text"
    if capabilities["strict_schema"]["status"] == "ok":
        preferred = "json_schema"
    elif capabilities["json_mode"]["status"] == "ok":
        preferred = "json_object"
    result = {"schema_version": "m3-model-capabilities-v1", "generated_at": _now(),
              "status": "ok" if basic_ok else capabilities["basic_text"]["status"],
              "model_configuration": settings.public_dict(), "capabilities": capabilities,
              "preferred_response_mode": preferred, "tools": "not_required_not_tested",
              "compatibility_notice": "仅证明所列请求与样例的实际表现；不是所有 schema 的兼容性保证，也不证明研究内容正确。",
              "verification_kind": client.verification_kind if client.call_count else "not_run",
              "usage": client.summary(),
              "records": client.records, "directory": str(directory), "result_file": str(directory / "result.json")}
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result


def choose_response_mode(settings: ModelSettings, capabilities: dict | Path | str | None = None) -> str:
    if capabilities is None or settings.response_mode == "text":
        return "text"
    if not isinstance(capabilities, dict):
        capabilities = json.loads(Path(capabilities).read_text(encoding="utf-8-sig"))
    expected = settings.public_dict()
    configured = capabilities.get("model_configuration", {})
    if (capabilities.get("schema_version") != "m3-model-capabilities-v1"
            or capabilities.get("verification_kind") != "real_network"
            or any(configured.get(k) != expected[k] for k in ("endpoint_url", "model_name", "protocol"))):
        raise ValueError("模型能力记录与当前配置不匹配或不是真实请求记录")
    probes = capabilities.get("capabilities", {})
    if settings.response_mode != "json_object" and probes.get("strict_schema", {}).get("status") == "ok":
        return "json_schema"
    if probes.get("json_mode", {}).get("status") == "ok":
        return "json_object"
    return "text"
