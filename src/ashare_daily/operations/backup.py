"""Versioned, verified local backups. Never restore over an existing directory."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import time
from types import MappingProxyType
from uuid import uuid4
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA = "m4-backup-v1"
_SCOPES = ("data/research", "outputs/research", "config",
           "data/engineering_validation/f3s", "outputs/engineering_validation/f3s")
_EXCLUDED_NAMES = {"credentials", "credentials.json", "secrets.json", "secrets.toml", "token.json"}
_SECRET = re.compile(rb"(?:\bsk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~-]{12,})", re.I)
_KEYS = re.compile(r"(?i)^(api[_-]?key|authorization|password|secret|access[_-]?token)$")
_REDACTED = {"", "[REDACTED]", "***", "已配置", "未配置"}


def _now():
    return datetime.now(SHANGHAI).isoformat()


def _io(path: Path) -> Path:
    """Use Windows extended paths internally without changing stored locators.

    This avoids a global LongPathsEnabled registry change. Logical paths remain
    ordinary paths for scope checks, manifests, and restore reference mapping.
    """
    value = str(path.absolute())
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def _hash(path: Path):
    digest = hashlib.sha256()
    with _io(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _no_links(path: Path):
    if any(_io(part).is_symlink() or (hasattr(part, "is_junction") and _io(part).is_junction())
           for part in (path, *path.parents)):
        raise ValueError("备份/恢复路径不能经过符号链接或 junction")


def _safe_relative(value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("归档路径必须是安全相对路径")
    raw = PurePosixPath(value)
    if raw.is_absolute() or any(p in {".", "..", ""} for p in value.split("/")):
        raise ValueError("拒绝归档路径穿越")
    for part in raw.parts:
        if (part.endswith((".", " ")) or any(ord(char) < 32 or char in '<>"|?*' for char in part)
                or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)):
            raise ValueError("归档路径含 Windows 保留名称或歧义字符")
    return Path(*raw.parts)


def _in_scope(relative: Path) -> bool:
    value = relative.as_posix()
    return (any(value.startswith(scope + "/") for scope in _SCOPES)
            or value.startswith("outputs/verification/") or value == "data/operations/runtime.sqlite3")


def _allowed(path: Path):
    return not (any(part.lower() in {"logs", "log", "__pycache__", ".git"} for part in path.parts)
                or path.name.lower().startswith(".env") or path.name.lower() in _EXCLUDED_NAMES
                or path.suffix.lower() in {".log", ".lock", ".pyc", ".pem", ".key"}
                or path.name.endswith(("-wal", "-shm", "-journal")))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if _KEYS.fullmatch(str(key)) and isinstance(item, str) and item not in _REDACTED:
                raise ValueError("备份范围含疑似明文凭据字段，已拒绝；请先在本机检查该文件")
            yield from _strings(item)


def _read_archive_file(path: Path):
    _no_links(path)
    before = _io(path).stat()
    body = _io(path).read_bytes()
    after = _io(path).stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("备份期间文件变化；请确认所有日任务共用锁后重试")
    if _SECRET.search(body):
        raise ValueError("备份范围含疑似明文凭据，已拒绝；不会把密钥写入备份")
    value = None
    if path.suffix.lower() == ".json":
        try:
            value = json.loads(body.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            # Corrupt historical reports are still preserved byte-for-byte.
            pass
        if value is not None:
            list(_strings(value))
    return body, value


def _database_copy(source: Path, destination: Path):
    _no_links(source)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as src:
        with closing(sqlite3.connect(destination, timeout=30)) as dst:
            src.backup(dst, pages=1024, sleep=0.02)
            # A standalone backup must not depend on transient WAL sidecars.
            dst.execute("PRAGMA journal_mode=DELETE")
            status = dst.execute("PRAGMA integrity_check").fetchall()
            if status != [("ok",)]:
                raise ValueError("SQLite 备份完整性检查失败")


def create_backup(project_root: Path, destination: Path) -> dict:
    """Caller must hold data/operations/daily.lock for a consistent report set.

    F3-S engineering writers must also be stopped or excluded with their
    data/engineering_validation/f3s/history.lock while this snapshot is made.

    Source SQLite files use the online backup API; WAL bytes are never copied.
    Every other file is immutable copied, with references followed only inside
    outputs/verification. No original history is deleted or rewritten.
    """
    project = Path(project_root).absolute()
    destination = Path(destination).absolute()
    _no_links(project)
    _no_links(destination)
    project = project.resolve()
    if _io(destination).exists():
        raise ValueError("备份目标必须是尚不存在的新目录")
    if any(destination.is_relative_to(project / scope) for scope in (*_SCOPES, "outputs/verification", "data/operations")):
        raise ValueError("备份目标不能位于备份输入范围内")
    primary_db = project / "data/research/market.sqlite3"
    if not _io(primary_db).is_file():
        raise ValueError("未找到项目真实行情数据库 data/research/market.sqlite3")
    queue = []
    for scope in _SCOPES:
        base = project / scope
        if not _io(base).exists():
            continue
        _no_links(base)
        for io_path in sorted(_io(base).rglob("*")):
            path = base / io_path.relative_to(_io(base))
            _no_links(path)
            if _io(path).is_file() and _allowed(path.relative_to(project)):
                queue.append(path)
    budget = project / "data/operations/runtime.sqlite3"
    if _io(budget).exists():
        queue.append(budget)
    _io(destination).mkdir(parents=True, exist_ok=False)
    files_root = destination / "files"
    _io(files_root).mkdir()
    seen, files, external, missing = set(), [], set(), set()
    while queue:
        path = queue.pop(0).absolute()
        _no_links(path)
        if not path.resolve().is_relative_to(project):
            raise ValueError("备份源路径超出项目目录")
        relative = path.relative_to(project)
        rel = relative.as_posix()
        if rel in seen:
            continue
        seen.add(rel)
        if not _allowed(relative):
            continue
        target = files_root / relative
        _io(target.parent).mkdir(parents=True, exist_ok=True)
        is_database = path.suffix.lower() in {".sqlite3", ".sqlite", ".db"}
        value = None
        if is_database:
            _database_copy(path, target)
        else:
            body, value = _read_archive_file(path)
            with _io(target).open("xb") as stream:
                stream.write(body)
        files.append({"path": rel, "sha256": _hash(target), "size": _io(target).stat().st_size,
                      "kind": "sqlite_backup" if is_database else "immutable_file"})
        if value is not None:
            for text in _strings(value):
                # Only exact file locator values are followed, never embedded
                # commands, URLs, arbitrary local paths, or external drives.
                if len(text) > 4096 or "\n" in text or text.startswith(("https:", "http:")):
                    continue
                looks_local = (text.lower().startswith(str(project).lower())
                               or text.startswith(("outputs/", "outputs\\")))
                if not looks_local:
                    if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"):
                        external.add("项目外的历史引用，未自动复制")
                    continue
                try:
                    candidate = Path(text.split("#", 1)[0])
                    if not candidate.is_absolute():
                        candidate = project / candidate
                    candidate = candidate.absolute()
                    if not candidate.is_relative_to(project):
                        external.add("项目外的历史引用，未自动复制")
                        continue
                    candidate_relative = candidate.relative_to(project)
                    if not _allowed(candidate_relative):
                        continue
                except (OSError, ValueError):
                    continue
                if candidate.is_relative_to(project / "outputs/verification"):
                    # Link escapes are a hard error, never an empty source.
                    _no_links(candidate)
                    if _io(candidate).is_file():
                        queue.append(candidate)
                    elif candidate.suffix:
                        missing.add(candidate_relative.as_posix())
    manifest = {"schema_version": SCHEMA, "created_at": _now(), "source_project_root": str(project),
                "files": sorted(files, key=lambda item: item["path"]), "file_count": len(files),
                "total_bytes": sum(item["size"] for item in files),
                "excluded": [".env 及凭据文件", "日志", "瞬态锁和 SQLite WAL/SHM", "未被研究存档引用的 verification 文件"],
                "external_reference_notes": sorted(external), "missing_referenced_files": sorted(missing),
                "budget_ledger_included": _io(budget).exists(),
                "reference_policy": "不可变内容保留原路径；恢复后用 restore-path-map.json 映射到新根目录"}
    with _io(destination / "manifest.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
    return {"status": "partial" if missing or external else "ok", "backup_directory": str(destination),
            "manifest": str(destination / "manifest.json"), "file_count": len(files),
            "total_bytes": manifest["total_bytes"], "missing_referenced_files": sorted(missing),
            "budget_ledger_included": _io(budget).exists(), "secrets_included": False}


def verify_backup(backup_dir: Path) -> dict:
    root = Path(backup_dir).absolute()
    _no_links(root)
    manifest_path = root / "manifest.json"
    _no_links(manifest_path)
    if not _io(manifest_path).is_file() or _io(manifest_path).stat().st_size > 32_000_000:
        raise ValueError("备份缺少有效 manifest")
    manifest = json.loads(_io(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA or not isinstance(manifest.get("files"), list):
        raise ValueError("备份 manifest 版本无效")
    seen = set()
    for item in manifest["files"]:
        relative = _safe_relative(item["path"])
        canonical = item["path"].casefold()
        if canonical in seen or not _allowed(relative) or not _in_scope(relative):
            raise ValueError("备份含重复路径或被禁止的敏感/瞬态文件")
        seen.add(canonical)
        path = root / "files" / relative
        _no_links(path)
        if not _io(path).is_file() or _io(path).stat().st_size != item["size"] or _hash(path) != item["sha256"]:
            raise ValueError("备份文件缺失或 SHA256 不匹配")
        if item["kind"] == "sqlite_backup":
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("备份 SQLite 完整性检查失败")
        elif item["kind"] != "immutable_file":
            raise ValueError("备份文件类型未知")
        else:
            _read_archive_file(path)
    if (len(seen) != manifest.get("file_count") or "data/research/market.sqlite3" not in seen
            or sum(item["size"] for item in manifest["files"]) != manifest.get("total_bytes")):
        raise ValueError("备份条目计数或主数据库无效")
    return manifest


def _publish_restored_stage(stage: Path, target: Path) -> None:
    """Bounded Windows directory-sharing retries; never overwrite or delete.

    All owned SQLite/files are closed before this point. Windows may still
    return access/sharing/lock errors for a just-verified directory. No POSIX
    errno, unrelated Windows error, or permanent permission is ignored.
    """
    delays = (0.05, 0.1, 0.2)
    for attempt in range(len(delays) + 1):
        _no_links(stage)
        _no_links(target)
        if _io(target).exists():
            raise ValueError("恢复目标在操作期间已出现，未覆盖")
        try:
            _io(stage).rename(_io(target))
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def restore_backup(backup_dir: Path, destination: Path) -> dict:
    target = Path(destination).absolute()
    _no_links(target)
    if _io(target).exists():
        raise ValueError("恢复目标必须是尚不存在的新目录，不允许覆盖当前项目或数据库")
    manifest = verify_backup(backup_dir)
    source = Path(backup_dir).absolute()
    if target.is_relative_to(source):
        raise ValueError("恢复目录不能位于备份包内")
    stage = target.with_name(target.name + ".restoring-" + uuid4().hex[:12])
    _io(stage).mkdir(parents=True, exist_ok=False)
    for item in manifest["files"]:
        relative = _safe_relative(item["path"])
        dest = stage / relative
        _io(dest.parent).mkdir(parents=True, exist_ok=True)
        with _io(source / "files" / relative).open("rb") as src, _io(dest).open("xb") as dst:
            shutil.copyfileobj(src, dst)
        if _hash(dest) != item["sha256"]:
            raise ValueError("恢复后文件哈希不符；保留独立 staging 目录供检查")
        if item["kind"] == "sqlite_backup":
            with closing(sqlite3.connect(dest.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("恢复后的 SQLite 完整性检查失败")
    mapping = {"schema_version": "m4-restore-map-v1", "source_project_root": manifest["source_project_root"],
               "restored_project_root": str(target), "restored_at": _now(),
               "paths": [item["path"] for item in manifest["files"]],
               "immutable_reference_policy": "未重写旧快照；映射仅定位文件，不改变证据内容和哈希"}
    _io(stage / "restore-path-map.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    _io(stage / "backup-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _publish_restored_stage(stage, target)
    return {"status": "ok", "restore_directory": str(target), "file_count": manifest["file_count"],
            "sqlite_integrity": "ok", "sha256_validation": "ok", "secrets_restored": False,
            "reference_map": str(target / "restore-path-map.json"),
            "budget_ledger_restored": manifest.get("budget_ledger_included", False)}


@lru_cache(maxsize=4)
def _validated_restore_index(mapping_bytes: bytes, manifest_bytes: bytes):
    """Cache only a fully validated immutable index, keyed by exact file bytes.

    Each resolver invocation rereads both files. Bytes keys are hashed and then
    compared by full content, so even a same-size/same-timestamp replacement
    cannot reuse another metadata version. Paths/stats are never cache keys.
    """
    mapping = json.loads(mapping_bytes.decode("utf-8"))
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if (not isinstance(mapping, dict) or not isinstance(manifest, dict)
            or mapping.get("schema_version") != "m4-restore-map-v1" or manifest.get("schema_version") != SCHEMA
            or mapping.get("source_project_root") != manifest.get("source_project_root")
            or not isinstance(mapping.get("source_project_root"), str)
            or not isinstance(mapping.get("paths"), list) or not isinstance(manifest.get("files"), list)):
        raise ValueError("恢复映射与备份清单不一致")
    windows_source = bool(re.match(r"^[A-Za-z]:", mapping["source_project_root"]) or mapping["source_project_root"].startswith("\\\\"))
    key = lambda value: value.casefold() if windows_source else value
    registered = {}
    for item in manifest["files"]:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                or type(item.get("size")) is not int or item["size"] < 0):
            raise ValueError("恢复清单文件字段无效")
        relative_name = _safe_relative(item["path"]).as_posix()
        if not _in_scope(Path(relative_name)) or not _allowed(Path(relative_name)) or key(relative_name) in registered:
            raise ValueError("恢复清单含越界、敏感或重复路径")
        registered[key(relative_name)] = (relative_name, item["size"], item["sha256"])
    mapped = [key(_safe_relative(name).as_posix()) for name in mapping["paths"]]
    if len(set(mapped)) != len(mapped) or set(mapped) != set(registered):
        raise ValueError("恢复映射登记与备份文件清单不一致")
    return mapping["source_project_root"], windows_source, MappingProxyType(registered)


def _read_restore_index(root: Path):
    metadata = []
    for name in ("restore-path-map.json", "backup-manifest.json"):
        path = root / name
        _no_links(path)
        if not _io(path).is_file() or _io(path).stat().st_size > 32_000_000:
            raise ValueError("恢复映射或备份清单缺失/超限")
        with _io(path).open("rb") as stream:
            body = stream.read(32_000_001)
        if len(body) > 32_000_000:
            raise ValueError("恢复映射或备份清单缺失/超限")
        metadata.append(body)
    return _validated_restore_index(*metadata)


def resolve_restored_reference(original_path: str, restore_root: Path) -> Path:
    """Resolve a registered archive, rechecking metadata content and source SHA."""
    from .paths import archive_relative_path
    root = Path(restore_root).absolute()
    _no_links(root)
    source_root, windows_source, registered = _read_restore_index(root)
    key = lambda value: value.casefold() if windows_source else value
    relative = _safe_relative(archive_relative_path(str(original_path), source_root).as_posix())
    item = registered.get(key(relative.as_posix()))
    if item is None:
        raise ValueError("引用未登记在已恢复产物中")
    # Use the manifest's actual spelling on case-sensitive destination hosts.
    relative = _safe_relative(item[0])
    target = root / relative
    _no_links(target)
    if (not _io(target).is_file() or _io(target).stat().st_size != item[1]
            or _hash(target) != item[2]):
        raise ValueError("已恢复引用文件缺失或 SHA256 不匹配")
    return target
