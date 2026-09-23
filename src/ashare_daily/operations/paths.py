"""Read relocated archives without rewriting immutable historical locators."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re


_F3S_MUTABLE_DATABASE = "data/engineering_validation/f3s/market.sqlite3"


def _pure(value: str):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("历史路径不能为空或包含空字符")
    if re.match(r"^[A-Za-z]:", value) or value.startswith("\\\\"):
        if value.startswith(("\\\\?\\", "\\\\.\\")):
            raise ValueError("历史定位不接受设备或扩展命名空间")
        return PureWindowsPath(value)
    return PurePosixPath(value)


def archive_relative_path(original_path: str, source_project_root: str) -> PurePosixPath:
    """Interpret the source OS syntax, independently of the running host OS."""
    source = _pure(source_project_root)
    if not source.is_absolute() or ".." in source.parts:
        raise ValueError("原项目根目录必须是规范绝对路径")
    value = _pure(original_path)
    # Relative archived paths use the source platform's separators.
    if not value.is_absolute() and not value.anchor:
        value = type(source)(original_path)
        if value.anchor or value.drive or ".." in value.parts:
            raise ValueError("拒绝相对历史路径穿越或驱动器相对路径")
        value = source / value
    if type(value) is not type(source) or not value.is_absolute() or ".." in value.parts:
        raise ValueError("历史路径与原项目平台或根目录不匹配")
    try:
        relative = value.relative_to(source)
    except ValueError:
        raise ValueError("引用不属于原项目") from None
    if not relative.parts:
        raise ValueError("历史引用必须定位文件，不能是项目根目录")
    return PurePosixPath(*relative.parts)


def _mapping_root(anchor: Path | None) -> Path | None:
    # A caller-owned anchor discovers the nearest restore metadata, never a
    # root suggested by the archived document itself.
    start = Path(anchor if anchor is not None else Path.cwd()).absolute()
    for root in (start, *start.parents):
        map_file = root / "restore-path-map.json"
        manifest = root / "backup-manifest.json"
        if (map_file.exists() or manifest.exists()
                or map_file.is_symlink() or manifest.is_symlink()):
            if not map_file.is_file() or not manifest.is_file():
                raise ValueError("恢复映射或备份清单缺失，拒绝猜测历史路径")
            return root
    return None


def _input_source_root(root: Path) -> str:
    """Validate restore metadata before choosing an explicit input's context."""
    from .backup import _allowed, _in_scope, _io, _no_links, _safe_relative

    metadata = []
    for name in ("restore-path-map.json", "backup-manifest.json"):
        path = root / name
        _no_links(path)
        if not _io(path).is_file() or _io(path).stat().st_size > 32_000_000:
            raise ValueError("恢复映射或备份清单缺失/超限")
        metadata.append(json.loads(_io(path).read_text(encoding="utf-8")))
    mapping, manifest = metadata
    if (not isinstance(mapping, dict) or not isinstance(manifest, dict)
            or mapping.get("schema_version") != "m4-restore-map-v1"
            or manifest.get("schema_version") != "m4-backup-v1"
            or not isinstance(mapping.get("source_project_root"), str)
            or mapping["source_project_root"] != manifest.get("source_project_root")
            or not isinstance(mapping.get("paths"), list)
            or not isinstance(manifest.get("files"), list)):
        raise ValueError("恢复映射与备份清单格式无效或不一致")
    source = _pure(mapping["source_project_root"])
    if not source.is_absolute() or ".." in source.parts:
        raise ValueError("原项目根目录必须是规范绝对路径")
    key = (lambda value: value.casefold()) if isinstance(source, PureWindowsPath) else (lambda value: value)
    registered = set()
    for item in manifest["files"]:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                or type(item.get("size")) is not int or item["size"] < 0
                or not isinstance(item.get("kind"), str)
                or item.get("kind") not in {"immutable_file", "sqlite_backup"}):
            raise ValueError("恢复清单文件字段无效")
        relative = _safe_relative(item["path"])
        name = key(relative.as_posix())
        if not _allowed(relative) or not _in_scope(relative) or name in registered:
            raise ValueError("恢复清单含越界、敏感或重复路径")
        registered.add(name)
    if not all(isinstance(name, str) for name in mapping["paths"]):
        raise ValueError("恢复映射路径必须为字符串")
    mapped = [key(_safe_relative(name).as_posix()) for name in mapping["paths"]]
    if len(set(mapped)) != len(mapped) or set(mapped) != registered:
        raise ValueError("恢复映射登记与备份文件清单不一致")
    return mapping["source_project_root"]


def _restored_f3s_database(relative: PurePosixPath, root: Path) -> Path | None:
    """Map the one resumable engineering DB without freezing its entire bytes.

    The original registration and storage identity remain mandatory. Individual
    immutable fact versions and source hashes are checked by the history reader.
    This is not a general exception for SQLite files or arbitrary native paths.
    """
    source = _input_source_root(root)
    windows_source = isinstance(_pure(source), PureWindowsPath)
    key = (lambda value: value.casefold()) if windows_source else (lambda value: value)
    if key(relative.as_posix()) != key(_F3S_MUTABLE_DATABASE):
        return None
    from .backup import _io, _no_links, _safe_relative
    manifest = json.loads(_io(root / "backup-manifest.json").read_text(encoding="utf-8"))
    item = next((item for item in manifest["files"] if key(item["path"]) == key(_F3S_MUTABLE_DATABASE)), None)
    if item is None or item["kind"] != "sqlite_backup":
        raise ValueError("工程行情数据库未登记为可恢复 SQLite 备份")
    target = root / _safe_relative(item["path"])
    _no_links(target)
    if not _io(target).is_file():
        raise ValueError("本恢复工程行情数据库缺失")
    with _io(target).open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise ValueError("本恢复工程行情数据库格式无效")
    return target


def resolve_input_path(value: str | Path) -> Path:
    """Resolve a caller-supplied input without inheriting unrelated cwd maps.

    A path in the original project's namespace is always an archived locator,
    even when its original disk is still accessible. Only an explicit native
    absolute path outside the current restored root may choose its own parent
    as context. References read from an archive must use resolve_archived_path
    with that archive's owner as anchor instead.
    """
    native = Path(value)
    root = _mapping_root(None)
    if root is not None:
        source = _pure(_input_source_root(root))
        original = _pure(str(value))
        # A restore may itself be nested below the old project directory. Its
        # native files belong to this restore, not to a second mapping beneath
        # the original root; frozen files still receive the archive hash check.
        if native.is_absolute() and native.resolve().is_relative_to(root.resolve()):
            return resolve_archived_path(value, anchor=root)
        if type(original) is type(source):
            try:
                original.relative_to(source)
            except ValueError:
                pass
            else:
                # Lexical containment is intentional: a source/../ traversal
                # must fail strict validation, never escape via native input.
                return resolve_archived_path(str(value), anchor=root)
        if not native.is_absolute():
            return resolve_archived_path(value, anchor=root)
    if native.is_absolute():
        owner_root = _mapping_root(native.parent)
        if owner_root is not None:
            _input_source_root(owner_root)
        return resolve_archived_path(value, anchor=native.parent)
    return resolve_archived_path(value)


def resolve_archived_path(value: str | Path, *, anchor: Path | None = None) -> Path:
    """Map old absolute references; ordinary native paths keep old behavior.

    Relocated frozen files are registered in both restore metadata and the backup
    manifest and are SHA256 checked. The exact resumable F3-S database keeps its
    registration while allowing newly appended business versions after restore.
    """
    raw = str(value)
    root = _mapping_root(anchor)
    if root is None:
        return Path(value)
    from .backup import _io, _no_links, resolve_restored_reference
    _no_links(root / "restore-path-map.json")
    if _io(root / "restore-path-map.json").stat().st_size > 32_000_000:
        raise ValueError("恢复映射超过大小限制")
    mapping = json.loads(_io(root / "restore-path-map.json").read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or not isinstance(mapping.get("source_project_root"), str):
        raise ValueError("恢复映射格式无效")
    original = _pure(raw)

    def native_path(path: Path) -> Path:
        _no_links(path)
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        scope_relative = relative.casefold() if isinstance(_pure(mapping["source_project_root"]), PureWindowsPath) else relative
        # Configuration, live databases, budgets and mutable indexes can change
        # normally after restoration. This helper protects frozen research
        # artifacts and adjusted responses when a reuse record now stores their
        # native location instead of the original foreign locator.
        engineering_scope = scope_relative.startswith(("outputs/engineering_validation/f3s/", "data/engineering_validation/f3s/"))
        immutable_scope = (relative.startswith(("outputs/research/", "outputs/verification/", "data/research/"))
            and path.suffix.lower() not in {".sqlite3", ".sqlite", ".db"}
            and path.name not in {"latest.json", "latest_report.json"}
            and not path.name.startswith("window-"))
        if engineering_scope and scope_relative != _F3S_MUTABLE_DATABASE:
            immutable_scope = True
        if immutable_scope:
            manifest_path = root / "backup-manifest.json"
            _no_links(manifest_path)
            if _io(manifest_path).stat().st_size > 32_000_000:
                raise ValueError("备份清单超过大小限制")
            manifest = json.loads(_io(manifest_path).read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
                raise ValueError("备份清单格式无效")
            windows_source = isinstance(_pure(mapping["source_project_root"]), PureWindowsPath)
            key = lambda name: name.casefold() if windows_source else name
            for item in manifest["files"]:
                if (isinstance(item, dict) and isinstance(item.get("path"), str)
                        and key(item["path"]) == key(relative)
                        and (item.get("kind") == "immutable_file" or engineering_scope)):
                    return resolve_restored_reference(relative, root)
        return path

    # Newly created native files remain available; registered immutable files
    # keep their backup integrity checks after a first successful migrated reuse.
    native = Path(value)
    if native.is_absolute() and native.resolve().is_relative_to(root.resolve()):
        return native_path(native)
    if not original.is_absolute() and not original.anchor:
        if native.resolve().is_relative_to(root.resolve()) and ".." not in native.parts:
            return native_path(native)
        relative = archive_relative_path(raw, mapping["source_project_root"])
        if relative.as_posix().casefold() == _F3S_MUTABLE_DATABASE:
            target = _restored_f3s_database(relative, root)
            if target is not None:
                return target
        return resolve_restored_reference(raw, root)
    # No fallback to an old drive, another project's directory, or another
    # host when a restore mapping has been explicitly installed.
    relative = archive_relative_path(raw, mapping["source_project_root"])
    if relative.as_posix().casefold() == _F3S_MUTABLE_DATABASE:
        target = _restored_f3s_database(relative, root)
        if target is not None:
            return target
    return resolve_restored_reference(raw, root)
