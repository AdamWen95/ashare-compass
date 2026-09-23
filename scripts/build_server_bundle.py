"""Build a portable, credential-free server bundle from an existing verified backup.

Only the Python standard library and the project's existing standard-library
backup verifier are used. This script never creates a backup or contacts a host.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import tarfile
import tomllib
from uuid import uuid4
from urllib.parse import parse_qsl, urlsplit


PROJECT_ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from ashare_daily.operations.backup import verify_backup  # noqa: E402


DIRECTORIES = ("src", "tests", "scripts", "docs", "config")
EXACT_FILES = ("streamlit_app.py", "pyproject.toml", "requirements.in", "requirements.lock", "requirements-linux.lock",
               "README.md", "AGENTS.md", ".gitignore", ".env.example", ".streamlit/config.toml")
EXCLUDED = {".env", ".venv", ".tools", "outputs", "data", "backups", "restore_checks", "cache", ".cache",
            ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
CREDENTIAL_NAMES = {"credentials", "credentials.json", "secrets.json", "secrets.toml", "token.json"}
MAX_SOURCE_BYTES = 64_000_000
TOKEN = re.compile(rb"\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+[A-Za-z0-9._~-]{12,}", re.I)
SECRET_FIELD = re.compile(r"(?i)^(?:(?:model|openai|anthropic|modex)[_-])?(?:api[_-]?key|authorization|password|secret|client[_-]secret|access[_-]token|refresh[_-]token)$")
PLACEHOLDERS = {"", "[REDACTED]", "***", "YOUR_API_KEY", "<YOUR_API_KEY>", "your-api-key", "你的完整密钥"}


def _io(path: Path) -> Path:
    value = str(path.absolute())
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def _no_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        target = _io(candidate)
        if target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction()):
            raise ValueError("拒绝符号链接或 junction 路径")


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("拒绝归档路径穿越")
    if "\\" in value or ":" in value:
        raise ValueError("归档成员必须是可移植的相对路径")
    for part in path.parts:
        if (part.endswith((".", " ")) or any(ord(char) < 32 or char in '<>"|?*' for char in part)
                or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)):
            raise ValueError("归档路径含 Windows 保留名称或歧义字符")
    return path.as_posix()


def _excluded(relative: Path) -> bool:
    if relative.as_posix() == ".env.example":
        return False
    return (any(part.lower() in EXCLUDED or part.lower().endswith((".egg-info", ".dist-info")) for part in relative.parts)
            or relative.name.lower().startswith(".env") or relative.name.lower() in CREDENTIAL_NAMES
            or relative.suffix.lower() in {".pyc", ".pyo", ".log", ".lock", ".key", ".pem", ".p12", ".pfx"}
            and relative.as_posix() not in {"requirements.lock", "requirements-linux.lock"})


def _regular(path: Path) -> os.stat_result:
    _no_links(path)
    info = _io(path).stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise ValueError("只归档普通独立文件，拒绝链接和特殊设备")
    return info


def _credential_fields(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_FIELD.fullmatch(str(key)) and item is not None and not (isinstance(item, str) and item in PLACEHOLDERS):
                raise ValueError("配置含非空凭据字段；请在本机移除后重试，密钥不应进入通用部署包")
            _credential_fields(item)
    elif isinstance(value, list):
        for item in value:
            _credential_fields(item)
    elif isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        parsed = urlsplit(value)
        if parsed.username or parsed.password:
            raise ValueError("配置网址含身份凭据；已拒绝打包")
        for key, item in parse_qsl(parsed.query):
            if SECRET_FIELD.fullmatch(key) and item not in PLACEHOLDERS:
                raise ValueError("配置网址查询参数含凭据；已拒绝打包")


def _check_content(body: bytes, relative: Path) -> None:
    # Offline test literals are code fixtures, not a permission to ship a key in
    # configuration. Configuration fields are checked separately below.
    is_test_code = relative.parts[0] == "tests" and relative.name.startswith("test_") and relative.suffix == ".py"
    for match in TOKEN.finditer(body):
        if is_test_code:
            # Existing security tests intentionally contain credential-shaped
            # negative examples. Preserve their Python source byte-for-byte.
            continue
        literal = match.group().upper()
        if not any(marker in literal for marker in (b"OFFLINE", b"EXAMPLE", b"PLACEHOLDER", b"DUMMY", b"FAKE", b"TEST")):
            raise ValueError("白名单源码含疑似凭据；已拒绝打包且不会打印该内容")
    is_configuration = relative.parts[0] in {"config", ".streamlit"} or relative.as_posix() in {".env.example", "pyproject.toml"}
    if not is_configuration:
        return
    try:
        text = body.decode("utf-8-sig")
        if relative.suffix.lower() == ".json":
            _credential_fields(json.loads(text))
        elif relative.suffix.lower() == ".toml":
            _credential_fields(tomllib.loads(text))
        else:
            for line in text.splitlines():
                if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip().strip('"\'')
                _credential_fields({key.strip(): value})
                if SECRET_FIELD.fullmatch(key.strip()):
                    if value not in PLACEHOLDERS:
                        raise ValueError("配置含非空凭据字段；密钥只能在服务器本机配置")
    except (UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("配置文件不能安全解析，未继续打包") from exc


@dataclass(frozen=True)
class InputFile:
    path: Path
    member: str
    size: int
    sha256: str
    kind: str


def _source_files(project: Path) -> list[InputFile]:
    selected: list[Path] = []

    def walk(directory: Path):
        _no_links(directory)
        for child in sorted(_io(directory).iterdir()):
            logical = directory / child.name
            _no_links(logical)
            relative = logical.relative_to(project)
            if _excluded(relative):
                continue
            if child.is_dir():
                walk(logical)
            else:
                selected.append(logical)

    for name in DIRECTORIES:
        base = project / name
        _no_links(base)
        if _io(base).is_dir():
            walk(base)
    for name in EXACT_FILES:
        path = project / name
        _no_links(path)
        if _io(path).is_file():
            selected.append(path)
    # Current and later milestone guides are the only permitted root glob.
    for path in _io(project).glob("QUICKSTART*.md"):
        selected.append(project / path.name)
    if not _io(project / "pyproject.toml").is_file() or not _io(project / "streamlit_app.py").is_file():
        raise ValueError("未找到项目必要入口；请从当前工程运行打包脚本")
    result, seen = [], set()
    for path in sorted(selected):
        relative = path.relative_to(project)
        if _excluded(relative):
            continue
        member = "project/" + _relative(relative.as_posix())
        if member.casefold() in seen:
            raise ValueError("源码存在大小写冲突或重复归档成员")
        seen.add(member.casefold())
        before = _regular(path)
        if before.st_size > MAX_SOURCE_BYTES:
            raise ValueError("单个白名单源码文件超过 64 MB，拒绝把非预期数据混入源码包")
        body = _io(path).read_bytes()
        after = _regular(path)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("源码在检查期间发生变化，请完成修改后重新打包")
        _check_content(body, relative)
        result.append(InputFile(path, member, len(body), hashlib.sha256(body).hexdigest(), "project_source"))
    return result


class HashingReader:
    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()
        self.count = 0

    def read(self, size=-1):
        body = self.stream.read(size)
        self.hash.update(body)
        self.count += len(body)
        return body


def _archive(stage: Path, inputs: list[InputFile]) -> None:
    # Deterministic metadata, regular members only; never tarfile.add() a tree.
    with _io(stage).open("xb") as output:
        with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for item in inputs:
                    before = _regular(item.path)
                    if before.st_size != item.size:
                        raise ValueError("输入在压缩前变化；未发布部署包")
                    info = tarfile.TarInfo(_relative(item.member))
                    info.size, info.mtime, info.uid, info.gid = item.size, 0, 0, 0
                    info.uname = info.gname = ""
                    info.mode = 0o644
                    with _io(item.path).open("rb") as stream:
                        reader = HashingReader(stream)
                        archive.addfile(info, reader)
                    after = _regular(item.path)
                    if (reader.count != item.size or reader.hash.hexdigest() != item.sha256
                            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                        raise ValueError("输入在压缩期间发生变化或哈希不匹配；未发布部署包")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _io(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_new(stage: Path, destination: Path) -> None:
    # An exclusive hard-link operation atomically refuses an existing name on
    # Windows and POSIX. Removing our temporary name leaves one regular file.
    _no_links(destination)
    os.link(_io(stage), _io(destination))
    _io(stage).unlink()


def build_bundle(backup: Path, output: Path, *, project_root: Path = PROJECT_ROOT) -> dict:
    project, backup, output = (Path(value).absolute() for value in (project_root, backup, output))
    for path in (project, backup, output):
        _no_links(path)
    project, backup, output = project.resolve(), backup.resolve(), output.resolve()
    if not output.name.endswith(".tar.gz"):
        raise ValueError("输出文件必须以 .tar.gz 结尾")
    _relative(output.name)
    side_manifest = output.with_name(output.name + ".manifest.json")
    checksum = output.with_name(output.name + ".sha256")
    for path in (output, side_manifest, checksum):
        _no_links(path)
        if _io(path).exists():
            raise ValueError("部署包及校验文件必须使用尚不存在的新路径，拒绝覆盖")
    if output.is_relative_to(backup) or any(output.is_relative_to(project / name) for name in (*DIRECTORIES, ".streamlit")):
        raise ValueError("输出不能位于备份或源码输入目录内")
    manifest = verify_backup(backup)
    inputs = _source_files(project)
    for item in manifest["files"]:
        relative = _relative(item["path"])
        path = backup / "files" / Path(*PurePosixPath(relative).parts)
        _regular(path)
        inputs.append(InputFile(path, "backup/files/" + relative, item["size"], item["sha256"], item["kind"]))
    manifest_path = backup / "manifest.json"
    manifest_info = _regular(manifest_path)
    inputs.append(InputFile(manifest_path, "backup/manifest.json", manifest_info.st_size, _hash_file(manifest_path), "backup_manifest"))
    inputs.sort(key=lambda item: item.member)
    if len({item.member.casefold() for item in inputs}) != len(inputs):
        raise ValueError("部署包存在重复成员或大小写冲突")
    _io(output.parent).mkdir(parents=True, exist_ok=True)
    stage = output.with_name(output.name + ".building-" + uuid4().hex)
    manifest_stage = stage.with_name(stage.name + ".manifest.json")
    checksum_stage = stage.with_name(stage.name + ".sha256")
    try:
        _archive(stage, inputs)
        archive_hash = _hash_file(stage)
        result = {"schema_version": "ashare-server-bundle-v1", "status": "ok", "created_at": datetime.now(timezone.utc).isoformat(),
                  "archive": str(output), "sha256": archive_hash, "compressed_bytes": _io(stage).stat().st_size,
                  "manifest_file": str(side_manifest), "checksum_file": str(checksum), "backup_verified": True,
                  "backup_schema": manifest["schema_version"], "member_count": len(inputs),
                  "source_file_count": sum(item.kind == "project_source" for item in inputs),
                  "backup_file_count": manifest["file_count"], "secrets_included": False,
                  "credential_check": "配置凭据字段及非测试源码明显凭据检查；tests/test_*.py 中故意构造的模拟凭据负例按原字节保留，不读取用户 .env。",
                  "top_level_directories": ["project", "backup"],
                  "files": [{"path": item.member, "size": item.size, "sha256": item.sha256, "kind": item.kind} for item in inputs]}
        with _io(manifest_stage).open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        with _io(checksum_stage).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(archive_hash + "  " + output.name + "\n")
        _publish_new(stage, output)
        _publish_new(manifest_stage, side_manifest)
        _publish_new(checksum_stage, checksum)
    finally:
        # Only files created by this invocation; no historical package deletion.
        for temporary in (stage, manifest_stage, checksum_stage):
            if _io(temporary).exists():
                _io(temporary).unlink()
    return {key: value for key, value in result.items() if key != "files"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="将白名单源码和已经验证的备份打成服务器迁移包；不含 .env、不联网。")
    parser.add_argument("--backup", type=Path, required=True, help="已经由 create_backup 创建的备份目录")
    parser.add_argument("--output", type=Path, required=True, help="尚不存在的新 .tar.gz 路径")
    args = parser.parse_args(argv)
    try:
        result = build_bundle(args.backup, args.output)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        # Do not echo arbitrary source/parser contents or a credential-bearing
        # exception. The exception class suffices alongside a local check hint.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "reason": "打包检查失败或路径不可用；检查备份完整性、白名单配置和新输出路径。未输出凭据。"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
