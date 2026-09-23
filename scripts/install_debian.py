"""Install a verified transfer bundle into a NEW private Debian user directory.

Bootstrap uses only Python's standard library, so Debian's Python 3.13 can run
this script while the application keeps its pinned, independent Python 3.12.
No system packages, profile files, credentials or services are modified.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

PYTHON = "3.12.14"
UV_VERSION = "0.12.11"
UV_URL = "https://files.pythonhosted.org/packages/05/20/0e9f74079ba34f12c5d5d01289dc8f8da94c11f855020bdfd7c5ddb412e1/uv-0.12.11-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
UV_SHA256 = "2c0d529c347eccaebaeec1be0e2a7a0a7d814128595b2549649060d2f75d0df6"
UV_BINARY_SHA256 = "215990511ac349fb8c46457774f5e65dbaf4aa313fac429fbd2e35254250c932"
UV_MEMBER = "uv-0.12.11.data/scripts/uv"
MARKER = ".server-install.json"


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def no_links(path: Path):
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise ValueError("安装路径不允许符号链接或 junction")


def relative(value: str) -> Path:
    p = PurePosixPath(value)
    if (not value or p.is_absolute() or "\\" in value or ":" in value
            or any(x in {"", ".", ".."} for x in value.split("/"))
            or any(ord(c) < 32 for c in value)):
        raise ValueError("部署清单中存在不安全的相对路径")
    return Path(*p.parts)


def inspect_bundle(bundle: Path) -> tuple[dict, str]:
    no_links(bundle)
    path = bundle / "bundle-manifest.json"
    no_links(path)
    if path.stat().st_size > 32_000_000:
        raise ValueError("部署清单过大")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "ashare-server-bundle-v1" or not manifest.get("backup_verified"):
        raise ValueError("不是已验证的服务器部署包")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != manifest.get("member_count"):
        raise ValueError("部署包成员数不一致")
    seen = set()
    for item in files:
        rel = relative(item["path"])
        if rel.parts[0] not in {"project", "backup"} or str(rel).casefold() in seen:
            raise ValueError("部署包范围或重复文件错误")
        seen.add(str(rel).casefold())
        if ".env" in rel.parts or ".venv" in rel.parts or ".git" in rel.parts:
            raise ValueError("部署包不得包含密钥文件或旧环境")
        source = bundle / rel
        no_links(source)
        if not source.is_file() or source.stat().st_size != item["size"] or sha(source) != item["sha256"]:
            raise ValueError("部署文件大小或 SHA256 不匹配：" + item["path"])
    if not {"project/scripts/install_debian.py", "project/requirements-linux.lock", "backup/manifest.json"}.issubset(
            {item["path"] for item in files}):
        raise ValueError("部署包缺少安装器、Linux锁文件或备份清单")
    return manifest, sha(path)


def plan(bundle: Path, destination: Path) -> dict:
    no_links(destination)
    manifest, manifest_hash = inspect_bundle(bundle)
    if destination == bundle or bundle.is_relative_to(destination) or destination.is_relative_to(bundle):
        raise ValueError("安装位置必须与解压位置分开")
    return {"operation": "preview", "external_calls": False, "bundle_root": str(bundle),
            "destination": str(destination), "destination_exists": destination.exists(),
            "manifest_sha256": manifest_hash, "member_count": manifest["member_count"],
            "python_version": PYTHON, "uv_version": UV_VERSION,
            "system_python_unchanged": True, "secrets_included": False, "services_enabled": False,
            "steps": ["验证包内文件", "恢复备份到新目录", "复制源码", "安装项目独立Python3.12",
                      "依Linux锁文件安装依赖", "离线doctor与日任务dry-run"],
            "required_network_on_install": ["files.pythonhosted.org", "PyPI", "Astral python-build-standalone releases on GitHub"],
            "application_external_calls_on_install": False}


def prepare(bundle: Path, destination: Path, *, resume: bool = False) -> dict:
    preview = plan(bundle, destination)
    manifest, _ = inspect_bundle(bundle)
    marker = destination / MARKER
    if destination.exists():
        no_links(marker)
        if not resume or not marker.is_file():
            raise ValueError("目标已经存在；仅允许对本安装器同一部署包的中断安装显式 --resume")
        prior = json.loads(marker.read_text(encoding="utf-8"))
        if prior.get("manifest_sha256") != preview["manifest_sha256"] or prior.get("destination") != str(destination):
            raise ValueError("恢复安装标识不匹配，拒绝覆盖现有项目")
        if prior.get("operation") == "installed":
            raise ValueError("安装已完成；不要用恢复安装覆盖正在使用的数据或环境")
        original_manifest = bundle / "backup/manifest.json"
        restored_manifest = destination / "backup-manifest.json"
        no_links(restored_manifest)
        if (not restored_manifest.is_file() or json.loads(restored_manifest.read_text(encoding="utf-8"))
                != json.loads(original_manifest.read_text(encoding="utf-8"))):
            raise ValueError("恢复备份清单已改变")
        for item in json.loads(original_manifest.read_text(encoding="utf-8"))["files"]:
            restored = destination / relative(item["path"])
            no_links(restored)
            if not restored.is_file() or restored.stat().st_size != item["size"] or sha(restored) != item["sha256"]:
                raise ValueError("已恢复的数据或预算发生改变，拒绝继续安装：" + item["path"])
    else:
        # Import only after the source files have passed bundle hash validation.
        sys.path.insert(0, str(bundle / "project/src"))
        from ashare_daily.operations.backup import restore_backup
        restore_backup(bundle / "backup", destination)
        marker.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in manifest["files"]:
        rel = relative(item["path"])
        if rel.parts[0] != "project":
            continue
        target = destination / Path(*rel.parts[1:])
        no_links(target)
        if target.exists():
            if not target.is_file() or sha(target) != item["sha256"]:
                raise ValueError("恢复的配置或已有源码与部署包冲突，拒绝覆盖：" + item["path"])
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with (bundle / rel).open("rb") as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
        target.chmod(0o600)
    return preview


def install_uv(destination: Path) -> Path:
    directory = destination / ".tools/bootstrap"
    no_links(directory)
    directory.mkdir(parents=True, exist_ok=True)
    binary, wheel = directory / "uv", directory / "uv.whl"
    for path in (binary, wheel):
        no_links(path)
    if binary.exists():
        if sha(binary) != UV_BINARY_SHA256:
            raise ValueError("已存在uv与固定版本哈希不符")
        binary.chmod(0o700)
        return binary
    if not wheel.exists():
        temporary = directory / "uv.download"
        no_links(temporary)
        # One bounded attempt. Network failures are resumed explicitly by user.
        try:
            with urllib.request.urlopen(UV_URL, timeout=45) as response, temporary.open("wb") as output:
                if response.status != 200:
                    raise ValueError("uv下载HTTP状态异常")
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 30_000_000:
                        raise ValueError("uv下载超过大小限制")
                    output.write(chunk)
            if sha(temporary) != UV_SHA256:
                raise ValueError("uv下载SHA256不匹配")
            temporary.replace(wheel)
        finally:
            temporary.unlink(missing_ok=True)
    if sha(wheel) != UV_SHA256:
        raise ValueError("uv缓存SHA256不匹配")
    with zipfile.ZipFile(wheel) as archive:
        data = archive.read(UV_MEMBER)
    if hashlib.sha256(data).hexdigest() != UV_BINARY_SHA256:
        raise ValueError("uv可执行文件SHA256不匹配")
    with binary.open("xb") as stream:
        stream.write(data)
    binary.chmod(0o700)
    return binary


def runtime_commands(destination: Path, uv: Path) -> list[list[str]]:
    python = str(destination / ".venv/bin/python")
    base = [str(uv), "--no-config", "--no-progress"]
    return [base + ["python", "install", PYTHON, "--no-bin"],
            base + ["venv", "--python", PYTHON, "--managed-python", str(destination / ".venv")],
            base + ["pip", "sync", "--python", python, "--require-hashes", "--default-index", "https://pypi.org/simple",
                    "--no-build", "requirements-linux.lock"],
            base + ["pip", "install", "--python", python, "--no-deps", "--no-build-isolation", "--offline", "-e", "."],
            [python, "-m", "pip", "check"], [python, "-m", "ashare_daily", "doctor", "--offline"],
            [python, "-m", "ashare_daily", "run-daily", "--dry-run"]]


def install_runtime(destination: Path, *, runner=subprocess.run) -> None:
    for relative_directory in (".venv", ".venv/bin", ".tools/python", ".tools/uv-cache", "outputs/verification/linux/install"):
        no_links(destination / relative_directory)
    # uv uses an interpreter symlink on Linux; it may point only into this
    # project's managed runtime, never an unrelated environment or executable.
    interpreter = destination / ".venv/bin/python"
    if interpreter.is_symlink() and not interpreter.resolve().is_relative_to((destination / ".tools/python").resolve()):
        raise ValueError("虚拟环境解释器不属于项目内受管Python")
    uv = install_uv(destination)
    # No credentials, custom credential-bearing indexes or active venv inherited.
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR") if key in os.environ}
    environment.update(UV_PYTHON_INSTALL_DIR=str(destination / ".tools/python"),
                       UV_CACHE_DIR=str(destination / ".tools/uv-cache"), UV_HTTP_TIMEOUT="45", UV_HTTP_RETRIES="1",
                       PYTHONUTF8="1", UV_LINK_MODE="copy")
    logs = destination / "outputs/verification/linux/install"
    logs.mkdir(parents=True, exist_ok=True)
    for number, command in enumerate(runtime_commands(destination, uv), 1):
        # Do not recreate an environment on a resumed download; verify version first.
        if number == 2 and (destination / ".venv/bin/python").exists():
            checked = runner([str(destination / ".venv/bin/python"), "-c", "import sys; assert sys.version_info[:3] == (3,12,14)"],
                             cwd=destination, env=environment, capture_output=True, timeout=30)
            if checked.returncode:
                raise ValueError("已有项目Python版本不符")
            continue
        print(f"步骤 {number}/7：{' '.join(command)}", flush=True)
        result = runner(command, cwd=destination, env=environment, capture_output=True, text=True, timeout=900)
        record = {"command": command, "exit_code": result.returncode,
                  "recorded_at": datetime.now(timezone.utc).isoformat(),
                  "stdout": result.stdout[-20000:], "stderr": result.stderr[-8000:]}
        log_file = logs / f"step-{number}.json"
        no_links(log_file)
        log_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        if result.returncode:
            raise ValueError(f"步骤{number}失败；查看本机 outputs/verification/linux/install/step-{number}.json。修复后用 --resume 继续")
    marker = destination / MARKER
    state = json.loads(marker.read_text(encoding="utf-8"))
    state.update(operation="installed", installed_at=datetime.now(timezone.utc).isoformat(),
                 application_external_calls=False, services_enabled=False)
    marker.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Debian私人部署；默认仅校验与预览，不安装、不联网。")
    parser.add_argument("--bundle-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--resume", action="store_true", help="继续同一部署包被网络中断的安装；不覆盖改动配置")
    args = parser.parse_args(argv)
    try:
        bundle, destination = args.bundle_root.absolute(), args.destination.expanduser().absolute()
        if args.install:
            if sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}:
                raise ValueError("实际安装仅支持Linux x86_64；其他平台可以运行预览和离线测试")
            if os.geteuid() == 0 or not destination.is_relative_to(Path.home()) or destination == Path.home():
                raise ValueError("请用普通用户安装到自己的独立项目目录，不要sudo")
            if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(destination)) or ".." in destination.parts:
                raise ValueError("目标目录只接受无空格或变量的绝对路径")
            os.umask(0o077)
            prepare(bundle, destination, resume=args.resume)
            install_runtime(destination)
            print("安装及离线检查完成。尚未配置密钥、未启用服务、未验证服务器真实采集和模型。")
        else:
            print(json.dumps(plan(bundle, destination), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        # No environment contents or arbitrary provider response printed.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, ValueError) else "路径、网络或子进程失败；检查安装记录。"}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
