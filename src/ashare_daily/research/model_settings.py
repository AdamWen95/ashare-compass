"""Local model configuration. No execution, interpolation, or secret logging."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from typing import Literal


def chat_endpoint(base_url: str) -> str:
    if not base_url:
        return ""
    parsed = urlsplit(base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() for c in base_url)):
        raise ValueError("MODEL_BASE_URL 必须为无凭据、查询参数和片段的 HTTPS 地址")
    path = parsed.path.rstrip("/")
    parts = path.split("/")
    if parts.count("v1") > 1 or ".." in parts or "%" in path or "\\" in path:
        raise ValueError("MODEL_BASE_URL 路径无效或重复 /v1")
    if path.endswith("/responses") or path.endswith("/messages"):
        raise ValueError("本轮只支持 Chat Completions；不自动切换协议")
    if not path:
        path = "/v1"
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    provider: str = ""
    base_url: str = ""
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False, exclude=True)
    model_name: str = ""
    protocol: Literal["chat_completions"] = "chat_completions"
    timeout_seconds: float = Field(default=30, ge=1, le=120)
    max_output_tokens: int = Field(default=1800, ge=16, le=16384)
    output_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    max_retries: int = Field(default=1, ge=0, le=2)
    max_calls: int = Field(default=6, ge=0, le=30)
    response_mode: Literal["auto", "text", "json_object", "json_schema"] = "auto"
    max_input_chars: int = Field(default=24000, ge=256, le=100000)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        chat_endpoint(value)
        return value.rstrip("/")

    @field_validator("provider", "model_name")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value):
            raise ValueError("模型和来源标识仅接受字母、数字及 _ . : / -")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        if any(c.isspace() for c in value.get_secret_value()):
            raise ValueError("MODEL_API_KEY 不可包含空白字符")
        return value

    @property
    def endpoint_url(self) -> str:
        return chat_endpoint(self.base_url)

    @property
    def missing_fields(self) -> list[str]:
        return [name for name, value in (("MODEL_BASE_URL", self.base_url),
                ("MODEL_NAME", self.model_name), ("MODEL_API_KEY", self.api_key.get_secret_value())) if not value]

    def public_dict(self) -> dict:
        return {**self.model_dump(mode="json"), "endpoint_url": self.endpoint_url,
                "key_present": bool(self.api_key.get_secret_value()), "missing_fields": self.missing_fields}


ENV_FIELDS = {
    "MODEL_PROVIDER": "provider", "MODEL_BASE_URL": "base_url", "MODEL_API_KEY": "api_key",
    "MODEL_NAME": "model_name", "MODEL_PROTOCOL": "protocol", "MODEL_TIMEOUT_SECONDS": "timeout_seconds",
    "MODEL_MAX_OUTPUT_TOKENS": "max_output_tokens", "MODEL_OUTPUT_TOKEN_PARAMETER": "output_token_parameter",
    "MODEL_MAX_RETRIES": "max_retries", "MODEL_MAX_CALLS": "max_calls", "MODEL_RESPONSE_MODE": "response_mode",
    "MODEL_MAX_INPUT_CHARS": "max_input_chars",
}


def _env_values(env_file: Path) -> dict[str, str]:
    if not env_file.exists():
        return {}
    if env_file.stat().st_size > 65536:
        raise ValueError("模型配置文件超过 64 KiB，未读取")
    values: dict[str, str] = {}
    for number, line in enumerate(env_file.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"配置文件第 {number} 行缺少等号（不显示内容）")
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in ENV_FIELDS:
            continue
        if key in values:
            raise ValueError(f"配置文件第 {number} 行重复模型字段（不显示内容）")
        if value[:1] in {"'", '"'}:
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].strip().startswith("#")):
                raise ValueError(f"配置文件第 {number} 行引号无效（不显示内容）")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        # Values are literal text, never shell code or ${VARIABLE} expansion.
        values[key] = value
    return values


def load_model_settings(env_file: Path | str = Path(".env"), *, include_environment: bool = True) -> ModelSettings:
    values = _env_values(Path(env_file))
    for key in ENV_FIELDS:
        if include_environment and key in os.environ:
            values[key] = os.environ[key]
    supplied = {ENV_FIELDS[key]: value for key, value in values.items()
                if value != "" or key in {"MODEL_PROVIDER", "MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME"}}
    try:
        return ModelSettings.model_validate(supplied)
    except ValidationError as exc:
        # Pydantic errors can carry original input values; report field names only.
        names = sorted({str(error["loc"][0]) for error in exc.errors(include_input=False)})
        raise ValueError("模型配置字段无效：" + ", ".join(names) + "（值已隐藏）") from None
