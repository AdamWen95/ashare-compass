"""Validated local deployment parameters; no credentials or host operations.

This standard-library-only module also travels beside standalone upgrade
installers. Importing it never reads a file or changes a running service.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re


SCHEMA = "deployment-local-v1"
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
FIELDS = frozenset({"project_root", "account", "ssh_host", "server_ip", "listen_port"})
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))


def _validated(name: str, value):
    if name not in FIELDS:
        raise ValueError("不支持的部署参数名称")
    if name == "project_root" and isinstance(value, Path):
        value = value.as_posix()
    valid = False
    if name == "listen_port":
        valid = type(value) is int and 1 <= value <= 65535
    elif isinstance(value, str) and value:
        if name == "project_root":
            path = PurePosixPath(value)
            valid = (value.startswith("/") and str(path) == value and len(path.parts) >= 4
                     and ".." not in path.parts and re.fullmatch(r"/[A-Za-z0-9_./-]+", value))
        elif name == "account":
            valid = re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value)
        elif name == "ssh_host":
            # An SSH config alias or hostname; shell syntax, scp paths and
            # embedded user/credentials are not accepted.
            valid = len(value) <= 253 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value)
        elif name == "server_ip":
            try:
                address = ipaddress.IPv4Address(value)
                valid = any(address in network for network in PRIVATE_NETWORKS)
            except ipaddress.AddressValueError:
                pass
    if not valid:
        raise ValueError(f"部署参数 {name} 无效（值未显示）")
    return value


def load_settings(path: Path | str | None = None, *, default_root: Path | str | None = None) -> dict:
    """Load an explicit file, or optional repository-local private settings.

    An explicitly named missing file always fails. An absent default returns
    an empty mapping so each command can require only its own needed fields.
    """
    explicit = path is not None
    target = Path(path) if explicit else Path(default_root or DEFAULT_ROOT) / ".local/deployment.json"
    try:
        with target.open("rb") as handle:
            raw = handle.read(65537)
    except FileNotFoundError:
        if not explicit:
            return {}
        raise ValueError("指定的部署配置文件不存在；未使用其他目标") from None
    except OSError:
        raise ValueError("无法读取部署配置文件（路径和内容未显示）") from None
    if len(raw) > 65536:
        raise ValueError("部署配置文件超过 64 KiB")
    try:
        values = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("部署配置不是有效的 UTF-8 JSON（内容未显示）") from None
    if not isinstance(values, dict) or set(values) - FIELDS - {"schema_version"}:
        raise ValueError("部署配置仅接受已声明的部署参数；不得包含凭据或未知字段")
    if "schema_version" in values and values["schema_version"] != SCHEMA:
        raise ValueError("不支持的部署配置 schema_version")
    return {name: value if name == "schema_version" else _validated(name, value)
            for name, value in values.items()}


def resolve_setting(settings: dict, key: str, explicit=None, required: bool = False):
    """CLI overrides local settings; missing required targets never fall back."""
    if key not in FIELDS:
        raise ValueError("不支持的部署参数名称")
    value = explicit if explicit is not None else settings.get(key)
    if value is None:
        if required:
            raise ValueError(f"缺少部署参数 {key}；请提供命令参数或本地部署配置")
        return None
    return _validated(key, value)


def add_deployment_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deployment-config", type=Path,
                        help="本地部署 JSON；默认读取项目 .local/deployment.json，不随源码分发")
