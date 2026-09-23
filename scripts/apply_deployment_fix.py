"""Preview or apply a hash-checked, code-only deployment fix; no network access."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from uuid import uuid4


def no_links(path: Path):
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("拒绝符号链接或 junction 路径")
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError("拒绝硬链接文件")


def read_file(path: Path, limit=10_000_000) -> bytes:
    no_links(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError("文件类型或大小不符合要求")
    body = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("校验期间文件发生变化")
    return body


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def replace_file(path: Path, body: bytes, mode: int):
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".deployment-fix-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        no_links(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_fix(project: Path, package: Path, *, apply: bool = False) -> dict:
    project, package = Path(project).absolute(), Path(package).absolute()
    no_links(project)
    no_links(package)
    if not project.is_dir() or not (project / "src/ashare_daily").is_dir():
        raise ValueError("请指定已安装的 ashare_daily 项目根目录")
    if apply and sys.platform == "linux" and os.geteuid() == 0:
        raise ValueError("请使用项目所属普通用户执行，不要 sudo 或 root")
    manifest = json.loads(read_file(package / "deployment-fix.json", 2_000_000))
    fix_id, entries = manifest.get("fix_id"), manifest.get("files")
    if not isinstance(fix_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", fix_id):
        raise ValueError("fix_id 无效")
    if not isinstance(entries, list) or not entries or len(entries) > 500:
        raise ValueError("修复清单必须含 1 至 500 个代码文件")
    checked, seen = [], set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("修复条目无效")
        name = item.get("path")
        if not isinstance(name, str) or not re.fullmatch(
            r"(?:src/ashare_daily/(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*|tests/test_[A-Za-z0-9_]+)\.py", name
        ) or name.casefold() in seen:
            raise ValueError("修复只允许唯一的 src/ashare_daily Python 文件及顶层 tests/test_*.py")
        seen.add(name.casefold())
        old_hash, new_hash = item.get("old_sha256"), item.get("new_sha256")
        if "old_sha256" not in item or not isinstance(new_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", new_hash):
            raise ValueError("修复 SHA256 字段无效")
        if old_hash is not None and (not isinstance(old_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", old_hash)):
            raise ValueError("原代码 SHA256 字段无效")
        body = read_file(package / "files" / name)
        if digest(body) != new_hash:
            raise ValueError("修复包新内容 SHA256 不匹配：" + name)
        target = project / name
        no_links(target)
        original = read_file(target) if target.exists() else None
        current_hash = digest(original) if original is not None else None
        if current_hash not in {old_hash, new_hash}:
            raise ValueError("目标代码与旧版/修复版 SHA256 均不符，未修改：" + name)
        checked.append({"path": name, "target": target, "body": body, "original": original,
                        "current": current_hash, "new": new_hash,
                        "mode": stat.S_IMODE(target.stat().st_mode) if original is not None else 0o600})
    changes = [item for item in checked if item["current"] != item["new"]]
    result = {"fix_id": fix_id, "status": "preview" if changes else "already_applied",
              "project": str(project), "files_checked": len(checked),
              "files_to_change": [item["path"] for item in changes],
              "external_calls": False, "data_or_credentials_modified": False}
    if not apply or not changes:
        return result
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]
    backup = project / "outputs/verification/linux/deployment-fixes" / fix_id / stamp
    no_links(backup)
    backup.mkdir(parents=True, exist_ok=False)
    result.update(backup_directory=str(backup), started_at=datetime.now(timezone.utc).isoformat())
    for item in changes:
        if item["original"] is not None:
            before = backup / "before" / item["path"]
            before.parent.mkdir(parents=True, exist_ok=True)
            with before.open("xb") as stream:
                stream.write(item["original"])
            before.chmod(0o600)
    (backup / "deployment-fix.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    changed = []
    try:
        for item in changes:
            target = item["target"]
            no_links(target)
            current = digest(read_file(target)) if target.exists() else None
            if current != item["current"]:
                raise ValueError("目标代码在应用前发生变化：" + item["path"])
            replace_file(target, item["body"], item["mode"])
            changed.append(item)
        result["status"] = "applied"
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        replace_file(backup / "result.json", json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"), 0o600)
    except Exception as error:
        rollback_failures = []
        for item in reversed(changed):
            try:
                if digest(read_file(item["target"])) != item["new"]:
                    raise ValueError("目标已被外部修改")
                if item["original"] is None:
                    item["target"].unlink()
                else:
                    replace_file(item["target"], item["original"], item["mode"])
            except Exception:
                rollback_failures.append(item["path"])
        result.update(status="rollback_incomplete" if rollback_failures else "rolled_back",
                      rollback_failures=rollback_failures, error_type=type(error).__name__)
        try:
            replace_file(backup / "result.json", json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"), 0o600)
        except Exception:
            pass  # Original code backups remain available even if storage has failed.
        raise ValueError("修复失败；" + ("回滚未完整完成" if rollback_failures else "本次已改代码已回滚") + "，查看 " + str(backup)) from error
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="代码修复默认仅预览；--apply 备份原代码后应用，不访问网络或读取密钥。")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = apply_fix(args.project.expanduser(), Path(__file__).absolute().parent, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__,
                          "reason": str(error) if isinstance(error, ValueError) else "修复包、文件权限或写入失败；未打印文件内容"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
