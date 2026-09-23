"""OFFLINE protocol fixtures; none of these tests prove provider compatibility."""
import json
import socket
from pathlib import Path
from urllib.error import URLError

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from ashare_daily.research.model import (ChatCompletionsModel, HTTPResponse, _NoRedirect,
    choose_response_mode, complete_validated, doctor_model, parse_json_object)
from ashare_daily.research.model_settings import ENV_FIELDS, ModelSettings, chat_endpoint, load_model_settings


KEY = "offline-secret-never-real"
MESSAGES = [{"role": "user", "content": "OFFLINE 测试"}]


@pytest.fixture(autouse=True)
def clear_model_env(monkeypatch):
    for key in ENV_FIELDS:
        monkeypatch.delenv(key, raising=False)


def settings(**kwargs):
    return ModelSettings(provider="offline_test", base_url="https://offline.example/v1", api_key=SecretStr(KEY),
                         model_name="offline-model", **kwargs)


def response(content='{"ok":true}', status=200, finish="stop", **kwargs):
    value = {"id": "offline-response", "model": "offline-model", "choices": [{"finish_reason": finish,
             "message": {"role": "assistant", "content": content, **kwargs}}],
             "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}}
    return HTTPResponse(status, json.dumps(value).encode())


class FakeTransport:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def __call__(self, url, headers, payload, timeout):
        self.requests.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def client(*results, **kwargs):
    fake = FakeTransport(*results)
    return ChatCompletionsModel(settings(**kwargs), transport=fake, sleep=lambda _: None), fake


@pytest.mark.parametrize("base", ["https://api.example.test/v1", "https://api.example.test/v1/",
    "https://api.example.test", "https://api.example.test/v1/chat/completions"])
def test_endpoint_once(base):
    assert chat_endpoint(base) == "https://api.example.test/v1/chat/completions"


@pytest.mark.parametrize("base", ["http://example.com/v1", "https://u:p@example.com/v1", "https://example.com?key=bad",
    "https://example.com/v1#bad", "https://example.com/v1/v1", "https://example.com/v1/responses",
    "https://example.com/v1/messages", "https://example.com/../v1", "https://example.com/%2e/v1", "https://example.com/a b"])
def test_invalid_endpoints(base):
    with pytest.raises(ValueError):
        chat_endpoint(base)


def test_env_absent_no_mutation(tmp_path, monkeypatch):
    result = load_model_settings(tmp_path / ".env")
    assert result.missing_fields == ["MODEL_BASE_URL", "MODEL_NAME", "MODEL_API_KEY"]
    assert not (tmp_path / ".env").exists()
    monkeypatch.setenv("MODEL_NAME", "exact-platform-id")
    assert load_model_settings(tmp_path / ".env").model_name == "exact-platform-id"


def test_env_literal_no_interpolation_and_environment_wins(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('MODEL_NAME="from-file" # example\nMODEL_API_KEY=literal-$UNCHANGED\nMODEL_BASE_URL=https://example.com/v1\n', encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setenv("MODEL_NAME", "gpt-5.6-sol")
    result = load_model_settings(path)
    assert result.model_name == "gpt-5.6-sol"
    assert result.api_key.get_secret_value() == "literal-$UNCHANGED"
    assert path.read_bytes() == original
    assert "literal-$UNCHANGED" not in repr(result)
    assert "literal-$UNCHANGED" not in json.dumps(result.public_dict())
    assert "api_key" not in result.model_dump()


@pytest.mark.parametrize("content", ["MODEL_NAME=a\nMODEL_NAME=b", 'MODEL_API_KEY="unclosed',
    "MODEL_API_KEY", "MODEL_TIMEOUT_SECONDS=secret-value", "MODEL_BASE_URL=http://secret-value", "MODEL_NAME=secret value"])
def test_invalid_env_does_not_reveal_values(tmp_path, content):
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_model_settings(path)
    assert "secret-value" not in str(exc.value)
    assert "unclosed" not in str(exc.value)


def test_minimal_request_no_optional_capabilities():
    c, fake = client(response("连接检查"))
    result = c.complete(MESSAGES)
    payload = fake.requests[0]["payload"]
    assert set(payload) == {"model", "messages", "max_tokens"}
    assert payload["model"] == "offline-model"
    assert result["status"] == "ok" and result["http_status"] == 200
    assert result["usage"] == {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}
    assert KEY not in json.dumps(c.records)
    assert c.summary()["currency_cost"] is None
    assert result["verification_kind"] == "offline_test"


@pytest.mark.parametrize("mode", ["json_object", "json_schema"])
def test_requested_json_wire_format(mode):
    c, fake = client(response())
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    c.complete(MESSAGES, response_mode=mode, schema=schema)
    wire = fake.requests[0]["payload"]["response_format"]
    assert wire["type"] == mode
    if mode == "json_schema":
        assert wire["json_schema"]["strict"] is True
        assert wire["json_schema"]["schema"] == schema


def test_missing_key_no_network():
    c = ChatCompletionsModel(ModelSettings(), transport=lambda *a: pytest.fail("network forbidden"))
    result = c.complete(MESSAGES)
    assert result["status"] == "missing_configuration" and c.call_count == 0


def test_auth_stops_subsequent_calls_and_redacts():
    c, fake = client(HTTPResponse(401, f"Bearer {KEY} sk-secretABC123 authentication failed".encode()))
    result = c.complete(MESSAGES)
    assert result["status"] == "authentication_failed"
    assert c.complete(MESSAGES)["status"] == "stopped"
    assert len(fake.requests) == 1
    assert KEY not in json.dumps(result) and "sk-secretABC123" not in json.dumps(result)


@pytest.mark.parametrize("code,status", [(403,"permission_denied"), (400,"request_rejected"),
    (404,"request_rejected"), (302,"redirect_blocked"), (307,"redirect_blocked"), (500,"server_error")])
def test_http_errors(code, status):
    c, _ = client(HTTPResponse(code, b"error"), max_retries=0)
    assert c.complete(MESSAGES)["status"] == status


def test_redirect_handler_never_forwards_credentials():
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example/") is None


@pytest.mark.parametrize("err,status", [(socket.timeout("SECRET"), "timeout"),
    (URLError(socket.timeout("SECRET")), "timeout"), (URLError("SECRET"), "network_error"),
    (RuntimeError("SECRET"), "transport_error")])
def test_transport_errors_no_exception_leak(err, status):
    c, _ = client(err, max_retries=0)
    result = c.complete(MESSAGES)
    assert result["status"] == status
    assert "SECRET" not in json.dumps(result)


def test_retry_counts_and_budget_include_attempts():
    c, fake = client(socket.timeout(), response(), response(), max_calls=2)
    result = c.complete(MESSAGES)
    assert result["status"] == "ok" and result["call_count"] == 2 and result["retries"] == 1
    assert c.complete(MESSAGES)["status"] == "budget_exhausted"
    assert len(fake.requests) == 2
    assert c.summary()["calls_without_usage"] == 1


def test_rate_limit_short_retry_after_obeyed():
    fake = FakeTransport(HTTPResponse(429, b"limited", {"Retry-After": "3"}), response())
    waits = []
    c = ChatCompletionsModel(settings(), transport=fake, sleep=waits.append)
    assert c.complete(MESSAGES)["status"] == "ok"
    assert waits == [3]


@pytest.mark.parametrize("retry_after", [None, "120", "Wed, 09 Sep 2026 12:00:00 GMT", "NaN", "-1"])
def test_unknown_or_long_rate_limit_stops(retry_after):
    c, fake = client(HTTPResponse(429, b"limited", {"retry-after": retry_after} if retry_after else {}))
    assert c.complete(MESSAGES)["status"] == "rate_limited"
    assert c.complete(MESSAGES)["status"] == "stopped"
    assert len(fake.requests) == 1


@pytest.mark.parametrize("fixture,expected", [(response("partial",finish="length"), "truncated"),
    (response("",refusal="cannot answer"), "refused"), (response("",finish="content_filter"), "refused"),
    (response("",tool_calls=[{"id":"no-execution"}]), "unexpected_tool_call"),
    (response(""), "empty_response"), (response("text",finish=None), "incomplete_response"),
    (HTTPResponse(200,b"<html>captcha</html>"),"invalid_response"),
    (HTTPResponse(200,b'[]'),"invalid_response")])
def test_invalid_refused_truncated_responses(fixture, expected):
    c, _ = client(fixture)
    assert c.complete(MESSAGES)["status"] == expected


def test_echoed_secret_in_success_response_is_redacted():
    c, _ = client(response("echo " + KEY))
    result = c.complete(MESSAGES)
    assert KEY not in json.dumps(result) and KEY not in json.dumps(c.records)


def test_limits_and_no_tools():
    c, fake = client(response(), max_input_chars=256)
    assert c.complete([{"role":"user","content":"x" * 257}])["status"] == "input_limit"
    assert c.complete([{"role":"tool","content":"anything"}])["status"] == "invalid_request"
    assert c.complete(MESSAGES, response_mode="json_schema")["status"] == "invalid_request"
    assert fake.requests == []


@pytest.mark.parametrize("text", ["[]", '```json\n{"ok":true}\n```', '{"ok":NaN}', "{", "null", '{"ok":true,"ok":false}'])
def test_plain_json_validation(text):
    with pytest.raises(ValueError):
        parse_json_object(text)


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answer: str


def test_single_format_repair_then_accept():
    c, fake = client(response("not json"), response('{"answer":"repaired"}'))
    result = complete_validated(c, MESSAGES, Output)
    assert result["status"] == "ok" and result["format_repairs"] == 1
    assert result["parsed"] == {"answer":"repaired"}
    assert len(fake.requests) == 2


def test_two_invalid_responses_degrade_no_third():
    c, fake = client(response("bad"), response('{"answer":1}'), response('{"answer":"unused"}'))
    result = complete_validated(c, MESSAGES, Output)
    assert result["status"] == "invalid_json" and result["parsed"] is None
    assert len(fake.requests) == 2


def test_transport_failure_not_format_repaired():
    c, fake = client(HTTPResponse(403,b"forbidden"))
    result = complete_validated(c, MESSAGES, Output)
    assert result["status"] == "permission_denied" and len(fake.requests) == 1


def test_do_not_send_key_in_prompt():
    c, fake = client()
    result = c.complete([{"role":"user", "content":"accidental " + KEY}])
    assert result["status"] == "invalid_request"
    assert fake.requests == [] and KEY not in json.dumps(result)


def test_doctor_independent_capabilities_and_no_tools(tmp_path):
    c, fake = client(response("连接检查"), response(), response(), HTTPResponse(400,b"unsupported response_format"))
    result = doctor_model(tmp_path, client=c)
    assert result["status"] == "ok"
    assert result["capabilities"]["strict_schema"]["status"] == "request_rejected"
    assert result["preferred_response_mode"] == "json_object"
    assert result["tools"] == "not_required_not_tested"
    assert len(fake.requests) == 4
    assert "response_format" not in fake.requests[0]["payload"]
    assert "response_format" not in fake.requests[1]["payload"]
    assert KEY not in Path(result["result_file"]).read_text(encoding="utf-8")
    assert result["verification_kind"] == "offline_test"


def test_doctor_stops_on_basic_failure(tmp_path):
    c, fake = client(HTTPResponse(401,b"failed"))
    result = doctor_model(tmp_path, client=c)
    assert result["capabilities"]["plain_json"]["status"] == "not_tested"
    assert len(fake.requests) == 1


def test_capability_selection_never_uses_mock_or_other_model(tmp_path):
    c, _ = client(response(), response(), response(), response())
    result = doctor_model(tmp_path, client=c)
    with pytest.raises(ValueError):
        choose_response_mode(c.settings, result)
    result["verification_kind"] = "real_network"  # synthetic envelope solely to test binding.
    assert choose_response_mode(c.settings, result) == "json_schema"
    result["model_configuration"]["model_name"] = "different"
    with pytest.raises(ValueError):
        choose_response_mode(c.settings, result)
    assert choose_response_mode(c.settings) == "text"
