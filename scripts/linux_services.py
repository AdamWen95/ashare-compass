"""Preview/manage this project's Linux user services, without loading credentials."""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import getpass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)
from deployment_settings import add_deployment_argument, load_settings, resolve_setting

DAILY = "ashare-daily-research-daily.service"
TIMER = "ashare-daily-research-daily.timer"
WEB = "ashare-daily-research-web.service"
UNITS = (DAILY, TIMER, WEB)
CALENDAR = "*-*-* 21:00:00 Asia/Shanghai"
CONFIRM_ACTIONS = {"install", "enable", "start-web", "run-daily"}


def checked_linux_path(value: str) -> str:
    path = PurePosixPath(value)
    # Fixed per-user deployment paths; disallow systemd expansion and shell syntax.
    if not value.startswith("/") or value != str(path) or ".." in path.parts or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise ValueError("Linux 项目路径必须是无空格、..、变量或控制字符的绝对路径")
    if len(path.parts) < 4 or path == PurePosixPath("/"):
        raise ValueError("请使用用户目录中的专用项目路径")
    return value


def build_units(project: str) -> dict[str, str]:
    project = checked_linux_path(project)
    owner = "# ashare-daily-research user-units v1 project=" + hashlib.sha256(project.encode()).hexdigest()
    base = f"{owner}\n# No secrets in units. The application reads its project .env.\n"
    daily = base + f"""[Unit]
Description=A-share evidenced daily research (project scope only)

[Service]
Type=oneshot
WorkingDirectory={project}
ExecStart={project}/.venv/bin/python -m ashare_daily run-daily --scheduled
UMask=0077
NoNewPrivileges=true
SuccessExitStatus=1 3
TimeoutStartSec=2h
TimeoutStopSec=30s
KillMode=control-group
Restart=no
StandardOutput=journal
StandardError=journal
"""
    timer = base + f"""[Unit]
Description=Start A-share daily research at 21:00 Beijing time

[Timer]
OnCalendar={CALENDAR}
Unit={DAILY}
Persistent=true
AccuracySec=1s
RandomizedDelaySec=0
WakeSystem=false

[Install]
WantedBy=timers.target
"""
    web = base + f"""[Unit]
Description=A-share read-only reports on localhost
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=exec
WorkingDirectory={project}
ExecStart={project}/.venv/bin/python -m streamlit run {project}/streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false --server.fileWatcherType none
UMask=0077
NoNewPrivileges=true
Restart=on-failure
RestartSec=10s
TimeoutStopSec=30s
KillMode=control-group
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""
    return {DAILY: daily, TIMER: timer, WEB: web}


def make_plan(project: str, account: str, *, now: datetime | None = None, offline: bool = False) -> dict:
    units = build_units(project)
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        raise ValueError("预览时间必须带时区")
    beijing = now.astimezone(ZoneInfo("Asia/Shanghai"))
    next_time = datetime.combine(beijing.date(), time(21), beijing.tzinfo)
    if next_time <= beijing:
        next_time += timedelta(days=1)
    return {
        "schema_version": "linux-user-services-v1", "operation": "preview",
        "verification_kind": "offline_render" if offline else "local_linux_preview",
        "account": account, "project_root": project, "python": project + "/.venv/bin/python",
        "working_directory": project, "units": units, "on_calendar": CALENDAR,
        "next_beijing": next_time.isoformat(), "next_local": next_time.astimezone(now.tzinfo).isoformat(),
        "current_local": now.isoformat(), "next_time_is_calculated_preview": True,
        "viewer_url": "http://127.0.0.1:8501", "web_read_only": True,
        "web_external_calls": False, "daily_calls": ["BaoStock configured sample and calendar", "Enabled M3 sources", "Configured Modex model within persisted daily budget"],
        "permission": "preview不写user-unit目录，不安装/启用。install、enable及启动命令需--confirm。",
        "linger": "unknown; use loginctl show-user <account> -p Linger. Script never enables lingering.",
        "limits": "Persistent只触发一次错过任务；应用仅处理当前北京时间日期，21点前not_due，自动最多2次/日，不回补多天。",
    }


def run_command(command: list[str], *, timeout: float = 30, check: bool = True) -> dict:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    record = {"command": command, "exit_code": result.returncode, "stdout": result.stdout[-30000:], "stderr": result.stderr[-5000:]}
    if check and result.returncode:
        raise RuntimeError("命令失败：" + " ".join(command) + "；" + result.stderr[-1500:])
    return record


def user_unit_directory() -> Path:
    home = Path.home().resolve()
    base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    if not base.is_absolute() or not base.resolve().is_relative_to(home):
        raise ValueError("用户 systemd 配置目录必须位于当前用户 HOME 内")
    directory = base / "systemd" / "user"
    if not directory.resolve().is_relative_to(home) or any(part.is_symlink() for part in (directory, *directory.parents) if part != home):
        raise ValueError("用户unit目录不能通过符号链接跳转")
    return directory


def assert_owned(directory: Path, units: dict[str, str], *, require_all: bool = False):
    for name, content in units.items():
        path = directory / name
        if (directory / (name + ".d")).exists():
            raise ValueError(f"发现额外 drop-in 配置，需人工审阅后才能管理：{name}.d")
        if path.is_symlink():
            raise ValueError(f"拒绝管理符号链接 unit：{name}")
        if not path.exists():
            if require_all:
                raise ValueError(f"尚未安装本项目 unit：{name}")
            continue
        if not path.is_file() or path.read_text(encoding="utf-8").splitlines()[:1] != content.splitlines()[:1]:
            raise ValueError(f"同名 unit 不属于本项目，拒绝更改：{name}")


def manage(action: str, plan: dict, directory: Path, *, confirmed: bool = False, runner=run_command) -> dict:
    if action in CONFIRM_ACTIONS and not confirmed:
        raise ValueError("先预览具体账号、时间、命令及外部调用；确认后显式使用 --confirm")
    units = plan["units"]
    if action == "preview":
        return plan
    assert_owned(directory, units, require_all=action in {"enable", "start-web", "run-daily"})
    commands, changed = [], []
    if action == "install":
        # Validate all generated files before replacing any existing user unit.
        with tempfile.TemporaryDirectory(prefix="ashare-systemd-check-") as temporary:
            staging = Path(temporary)
            for name, content in units.items():
                (staging / name).write_text(content, encoding="utf-8", newline="\n")
            commands.append(runner(["systemd-analyze", "--user", "verify", *[str(staging / name) for name in UNITS]]))
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in units.items():
            path = directory / name
            if path.is_file() and path.read_text(encoding="utf-8") == content:
                continue
            temporary = directory / ("." + name + ".tmp-" + str(os.getpid()))
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
            changed.append(name)
        commands.append(runner(["systemctl", "--user", "daemon-reload"]))
    elif action == "enable":
        commands.append(runner(["systemctl", "--user", "enable", "--now", TIMER, WEB]))
    elif action == "start-web":
        commands.append(runner(["systemctl", "--user", "start", WEB]))
    elif action == "run-daily":
        commands.append(runner(["systemctl", "--user", "start", "--no-block", DAILY]))
    elif action == "disable":
        commands.append(runner(["systemctl", "--user", "disable", "--now", TIMER, WEB]))
    elif action == "uninstall":
        state = runner(["systemctl", "--user", "show", "--property=ActiveState", "--value", DAILY])["stdout"].strip()
        if state not in {"inactive", "failed"}:
            raise ValueError("日任务仍在运行；请等待其结束再卸载，未停止研究进程")
        commands.append(runner(["systemctl", "--user", "disable", "--now", TIMER, WEB]))
        for name in UNITS:
            path = directory / name
            if path.is_file():
                path.unlink()
                changed.append(name)
        commands.append(runner(["systemctl", "--user", "daemon-reload"]))
    elif action == "status":
        commands.append(runner(["systemctl", "--user", "show", *UNITS, "--property=Id,LoadState,ActiveState,SubState,UnitFileState,MainPID,ExecMainPID,ExecMainCode,ExecMainStatus,Result,LastTriggerUSec,NextElapseUSecRealtime,FragmentPath"], check=False))
        commands.append(runner(["systemctl", "--user", "list-timers", "--all", "--no-pager", TIMER], check=False))
        commands.append(runner(["loginctl", "show-user", plan["account"], "-p", "Linger"], check=False))
    elif action == "logs":
        commands.append(runner(["journalctl", "--user", "--no-pager", "-n", "80", "-u", DAILY, "-u", WEB], check=False))
    else:
        raise ValueError("未知服务管理动作")
    return {"operation": action, "changed_units": changed, "commands": commands,
            "unit_directory": str(directory), "note": "unit或进程成功不等于研究完整；请核对对应run result中的各模块状态。"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="中文说明见 docs/25_DEBIAN_DEPLOYMENT.md")
    parser.add_argument("action", nargs="?", default="preview", choices=("preview", "install", "enable", "status", "start-web", "run-daily", "disable", "uninstall", "logs"))
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--render-only", action="store_true", help="仅离线生成预览，不接触systemd；可在Windows核对Linux配置")
    parser.add_argument("--account", help="本地部署配置或当前普通用户；真实Linux操作强制核对当前账号")
    parser.add_argument("--output", type=Path)
    add_deployment_argument(parser)
    args = parser.parse_args(argv)
    try:
        if args.render_only and args.action != "preview":
            raise ValueError("--render-only 只允许 preview")
        if not args.render_only and sys.platform != "linux":
            raise ValueError("真实服务管理仅在Linux运行；Windows可用preview --render-only")
        settings = load_settings(args.deployment_config)
        account = resolve_setting(settings, "account", args.account)
        if account is None:
            account = resolve_setting({}, "account", getpass.getuser(), required=True)
            if account == "root":
                raise ValueError("请提供项目普通用户账号，不要使用root")
        if not args.render_only and (os.geteuid() == 0 or getpass.getuser() != account):
            raise ValueError("请以已确认普通用户账号运行，不要sudo执行本脚本")
        plan = make_plan(args.project_root, account, offline=args.render_only)
        if not args.render_only:
            project = Path(args.project_root)
            if project.resolve() != project or not project.is_relative_to(Path.home().resolve()):
                raise ValueError("真实部署须位于当前用户HOME内，项目路径不能是符号链接")
            if args.action in CONFIRM_ACTIONS and not (project / ".venv/bin/python").is_file():
                raise ValueError("请先完成项目Python3.12 .venv安装")
        directory = Path(".") if args.action == "preview" else user_unit_directory()
        result = manage(args.action, plan, directory, confirmed=args.confirm)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n", encoding="utf-8")
            args.output.chmod(0o600)
        print(text)
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc), "credentials_read": False}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
