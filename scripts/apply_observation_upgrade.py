"""Preview/apply the daily observation upgrade to an existing Linux installation.

Uses the existing daily lock and timer. Never reads credentials, copies data,
starts a research run, installs dependencies, or enables a second scheduler.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import urlopen
from uuid import uuid4

_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)
from deployment_settings import add_deployment_argument, load_settings, resolve_setting
from apply_deployment_fix import digest, no_links, read_file, replace_file
from apply_research_upgrade import change_files, restore_files, run_regression
from linux_services import build_units, DAILY, TIMER, WEB

SCHEMA = "observation-upgrade-v1"
DAILY_CONFIG = "config/sector_observation_daily.json"
TEST_SUPPORT = "outputs/verification/f2s1/run_command.py"
BACKUP_BASE = "outputs/verification/linux/observation-upgrades"


def allowed_path(name):
    if not isinstance(name, str):
        return False
    return bool(name in {"streamlit_app.py", TEST_SUPPORT} or re.fullmatch(
        r"(?:src/ashare_daily/(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*\.py"
        r"|src/ashare_daily/(?:fixtures/[A-Za-z0-9_-]+\.json|reports/templates/[A-Za-z0-9_-]+\.html)"
        r"|tests/(?:test_[A-Za-z0-9_]+|conftest)\.py"
        r"|scripts/[A-Za-z_][A-Za-z0-9_-]*\.(?:py|sh|ps1)"
        r"|config/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.json)", name))


def inspect_upgrade(project, package):
    no_links(project)
    no_links(package)
    if not (project / "src/ashare_daily").is_dir() or not (project / "config/m4.json").is_file():
        raise ValueError("目标不是已安装的 A 股研究项目")
    manifest = json.loads(read_file(package / "upgrade.json", 2_000_000))
    if manifest.get("schema_version") != SCHEMA or manifest.get("daily_config") != DAILY_CONFIG:
        raise ValueError("观察版升级清单或日任务配置不符")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 500:
        raise ValueError("升级文件数量超出有限范围")
    seen, checked = set(), []
    for entry in entries:
        name = entry.get("path", "")
        if not allowed_path(name) or name.casefold() in seen:
            raise ValueError("非法或重复升级路径：" + str(name))
        seen.add(name.casefold())
        expected, new_hash = entry.get("accepted_previous_sha256"), entry.get("sha256")
        if (not isinstance(expected, list) or not expected or len(expected) > 32
                or any(h is not None and (not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h)) for h in expected)
                or not isinstance(new_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", new_hash)):
            raise ValueError("升级哈希结构无效")
        body = read_file(package / "files" / name)
        if digest(body) != new_hash:
            raise ValueError("包内文件校验失败：" + name)
        if name.endswith(".py"):
            compile(body, name, "exec")
        elif name.endswith(".json"):
            json.loads(body)
        target = project / name
        no_links(target)
        before = read_file(target) if target.exists() else None
        current = digest(before) if before is not None else None
        if current not in [*expected, new_hash]:
            raise ValueError("服务器文件有未识别修改，未覆盖：" + name)
        checked.append({"path": name, "target": target, "body": body, "before": before,
                        "previous_sha256": current, "sha256": new_hash,
                        "mode": target.stat().st_mode & 0o777 if target.exists() else 0o600})
    if DAILY_CONFIG.casefold() not in seen or TEST_SUPPORT.casefold() not in seen:
        raise ValueError("缺少观察版日任务配置或回归测试支持文件")
    return manifest, checked


def daily_unit(project):
    original = build_units(str(project))[DAILY]
    return original.replace(" -m ashare_daily run-daily --scheduled\n",
                            " -m ashare_daily run-daily --config " + DAILY_CONFIG + " --scheduled\n")


def inspect_units(project, directory):
    no_links(directory)
    expected = build_units(str(project))
    result = {}
    for name, original in expected.items():
        path = directory / name
        if (directory / (name + ".d")).exists():
            raise ValueError("发现额外 unit drop-in，未修改：" + name)
        body = read_file(path)
        normalized = body.decode("utf-8").replace("\r\n", "\n")
        permitted = {original, daily_unit(project)} if name == DAILY else {original}
        if normalized not in permitted:
            raise ValueError("现有 unit 与已核验项目定义不同，未修改：" + name)
        result[name] = {"body": body, "sha256": digest(body), "mode": path.stat().st_mode & 0o777}
    return result


def systemd(*args, check=True):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                          check=check, timeout=30)


def service_state(unit):
    result = systemd("show", unit, "--property=LoadState,ActiveState,SubState,UnitFileState,FragmentPath,DropInPaths")
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if state.get("LoadState") != "loaded" or state.get("DropInPaths"):
        raise ValueError("既有服务未正常加载或含额外配置：" + unit)
    return state


def check_environment(project, manifest):
    python = project / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("找不到现有项目 Python 环境")
    pins = manifest.get("required_versions")
    if not isinstance(pins, dict) or not pins or len(pins) > 20 or any(
            not re.fullmatch(r"[A-Za-z0-9_-]+", str(k)) or not re.fullmatch(r"[0-9A-Za-z.+_-]+", str(v)) for k, v in pins.items()):
        raise ValueError("依赖锁定信息无效")
    code = ("import importlib.metadata,json,sys; "
            "names=json.loads(sys.argv[1]); "
            "print(json.dumps({'python':list(sys.version_info[:2]),'versions':{n:importlib.metadata.version(n) for n in names}}))")
    result = subprocess.run([str(python), "-c", code, json.dumps(list(pins))], cwd=project,
                            capture_output=True, text=True, check=True, timeout=30)
    actual = json.loads(result.stdout)
    if actual.get("python") != [3, 12] or actual.get("versions") != pins:
        raise ValueError("服务器 Python 或已安装依赖不匹配；升级器不联网安装或修改环境")
    if manifest.get("required_node_runtime") is not None:
        check_node_runtime(project, manifest["required_node_runtime"])
    return python


def check_node_runtime(project, pin):
    if (not isinstance(pin, dict) or pin.get("schema_version") != "node-runtime-v1"
            or pin.get("platform") != "linux-x64" or pin.get("executable") != ".tools/node/bin/node"
            or not re.fullmatch(r"v\d+\.\d+\.\d+", str(pin.get("version", "")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(pin.get("executable_sha256", "")))):
        raise ValueError("项目 Node 锁定配置无效")
    node = project / pin["executable"]
    no_links(node)
    if not node.is_file() or not 0 < node.stat().st_size <= 200_000_000:
        raise ValueError("缺少项目 Node 运行时；先运行 install_node_runtime.py，尚未修改应用")
    if digest(node.read_bytes()) != pin["executable_sha256"]:
        raise ValueError("项目 Node 运行时哈希不匹配；尚未修改应用")
    result = subprocess.run([str(node), "--version"], capture_output=True, text=True, timeout=5, check=True)
    if result.stdout.strip() != pin["version"]:
        raise ValueError("项目 Node 运行时版本不匹配；尚未修改应用")


def check_web():
    for _ in range(15):
        try:
            with urlopen("http://127.0.0.1:8501/_stcore/health", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("新版网页健康检查未通过")


def verify_unit_file(project, backup):
    validation = backup / "unit-validation"
    validation.mkdir()
    for name, body in build_units(str(project)).items():
        (validation / name).write_text(daily_unit(project) if name == DAILY else body, encoding="utf-8")
    subprocess.run(["systemd-analyze", "--user", "verify", *[str(validation / name) for name in (DAILY, TIMER, WEB)]],
                   capture_output=True, text=True, check=True, timeout=30)


def apply_changes(project, changes, unit_directory, units, backup, python, *, regression=run_regression):
    """Transaction boundary, exercised offline with service/network substitutes."""
    states = {name: service_state(name) for name in (DAILY, TIMER, WEB)}
    if states[DAILY]["ActiveState"] not in {"inactive", "failed"}:
        raise ValueError("日任务正在运行或切换中，未修改")
    for name in (DAILY, TIMER, WEB):
        if states[name].get("FragmentPath") != str(unit_directory / name):
            raise ValueError("已加载 unit 路径与核验路径不同：" + name)
    web_active = states[WEB]["ActiveState"] == "active"
    applied, unit_changed = False, False
    desired = daily_unit(project).encode("utf-8")
    result = {"status": "pending", "backup_directory": str(backup), "daily_config": DAILY_CONFIG,
              "model_refresh": "not_run", "timer_modified": False, "service_states_before": states}
    if web_active:
        systemd("stop", WEB)
    try:
        # Recheck all unit bytes after obtaining the caller's daily lock.
        for name, value in units.items():
            if digest(read_file(unit_directory / name)) != value["sha256"]:
                raise ValueError("应用前 unit 发生变化：" + name)
        change_files(changes, backup)
        applied = True
        (backup / "daily.service.before").write_bytes(units[DAILY]["body"])
        (backup / "unit-rollback.json").write_text(json.dumps({"name": DAILY, "previous_sha256": units[DAILY]["sha256"],
            "sha256": digest(desired), "mode": units[DAILY]["mode"]}), encoding="utf-8")
        check = regression(python, project, backup)
        result["pytest_exit_code"] = check.returncode
        if check.returncode:
            raise RuntimeError("服务器回归未通过，详见 " + str(backup / "pytest.txt"))
        verify_unit_file(project, backup)
        if units[DAILY]["body"] != desired:
            if digest(read_file(unit_directory / DAILY)) != units[DAILY]["sha256"]:
                raise ValueError("发布前 daily unit 发生变化")
            replace_file(unit_directory / DAILY, desired, units[DAILY]["mode"])
            unit_changed = True
            systemd("daemon-reload")
        for name in (TIMER, WEB):
            if digest(read_file(unit_directory / name)) != units[name]["sha256"]:
                raise ValueError("升级期间非目标 unit 发生变化：" + name)
        if web_active:
            systemd("start", WEB)
            check_web()
        after = {name: service_state(name) for name in (DAILY, TIMER, WEB)}
        for key in ("ActiveState", "UnitFileState"):
            if after[TIMER].get(key) != states[TIMER].get(key):
                raise ValueError("既有 timer 状态发生变化，未确认升级成功")
        result.update(status="applied_and_verified", service_states_after=after)
    except BaseException:
        if web_active:
            systemd("stop", WEB, check=False)
        rollback_errors = []
        if unit_changed:
            try:
                if digest(read_file(unit_directory / DAILY)) != digest(desired):
                    raise ValueError("回滚前 daily unit 发生外部变化")
                replace_file(unit_directory / DAILY, units[DAILY]["body"], units[DAILY]["mode"])
                systemd("daemon-reload")
            except Exception as exc:
                rollback_errors.append(type(exc).__name__ + ": " + str(exc))
        if applied:
            try:
                restore_files(changes)
            except Exception as exc:
                rollback_errors.append(type(exc).__name__ + ": " + str(exc))
        # change_files also rolls back a partial copy internally. Verify that
        # boundary before calling any failed transaction successfully restored.
        for item in changes:
            try:
                current = digest(read_file(item["target"])) if item["target"].exists() else None
                if current != item["previous_sha256"]:
                    rollback_errors.append("源码尚未恢复：" + item["path"])
            except Exception as exc:
                rollback_errors.append("源码恢复核验失败：" + item["path"] + "：" + type(exc).__name__)
        try:
            if digest(read_file(unit_directory / DAILY)) != units[DAILY]["sha256"]:
                rollback_errors.append("daily unit尚未恢复；保留外部改动，需核对备份")
        except Exception as exc:
            rollback_errors.append("daily unit恢复核验失败：" + type(exc).__name__)
        if web_active:
            restarted = systemd("start", WEB, check=False)
            if restarted.returncode:
                rollback_errors.append("原网页启动失败")
        result.update(status="rollback_failed" if rollback_errors else "rolled_back", rollback_errors=rollback_errors)
        if backup.exists():
            (backup / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        raise
    (backup / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, help="Linux目标项目绝对路径；也可由本地部署配置提供")
    parser.add_argument("--package", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--apply", action="store_true")
    add_deployment_argument(parser)
    args = parser.parse_args(argv)
    settings = load_settings(args.deployment_config)
    project = Path(resolve_setting(settings, "project_root", args.project, required=True)).absolute()
    package = args.package.absolute()
    manifest, entries = inspect_upgrade(project, package)
    changes = [item for item in entries if item["previous_sha256"] != item["sha256"]]
    print(json.dumps({"status": "preview", "project": str(project), "files_to_change": [c["path"] for c in changes],
                      "existing_daily_exec_start": str(project / ".venv/bin/python") + " -m ashare_daily run-daily --config " + DAILY_CONFIG + " --scheduled",
                      "timer_created": False, "timer_modified": False, "external_calls": False}, ensure_ascii=False), flush=True)
    if not args.apply:
        return 0
    if sys.platform != "linux" or os.geteuid() == 0 or project.stat().st_uid != os.geteuid():
        raise ValueError("部署应用仅供 Linux 项目所属普通账号，不要 sudo")
    python = check_environment(project, manifest)
    unit_directory = Path.home() / ".config/systemd/user"
    units = inspect_units(project, unit_directory)
    sys.path.insert(0, str(project / "src"))
    from ashare_daily.operations.lock import ProcessLock
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    with ProcessLock(project / "data/operations/daily.lock", "observation-upgrade-" + stamp):
        if not changes and units[DAILY]["body"] == daily_unit(project).encode("utf-8"):
            result = {"status": "already_applied", "model_refresh": "not_run", "timer_modified": False}
        else:
            result = apply_changes(project, changes, unit_directory, units, project / BACKUP_BASE / stamp, python)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        raise SystemExit(2)
