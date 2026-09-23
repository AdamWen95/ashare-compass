"""Build a credential-free, hash-checked upgrade for the existing daily timer."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tomllib
import zipfile

_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)
from deployment_settings import add_deployment_argument, load_settings, resolve_setting
from apply_observation_upgrade import SCHEMA, DAILY_CONFIG, TEST_SUPPORT, allowed_path
from apply_deployment_fix import digest, read_file
from build_server_bundle import _check_content

ROOT = Path(__file__).resolve().parents[1]
KNOWN_ARCHIVES = ("ashare-debian-20260910.tar.gz", "ashare-debian-fix-20260910.tar.gz",
                  "ashare-research-upgrade-20260911T034126Z.tar.gz", "ashare-research-upgrade-20260911T044052Z.tar.gz")
HELPERS = ("apply_observation_upgrade.py", "apply_research_upgrade.py", "apply_deployment_fix.py", "linux_services.py", "deployment_settings.py")


def source_files(root, *, exclude=None):
    excluded = Path(exclude).resolve() if exclude is not None else None
    paths = [path for folder in ("src/ashare_daily", "config", "scripts", "tests")
             for path in (root / folder).rglob("*") if path.is_file() and allowed_path(path.relative_to(root).as_posix())]
    paths += [root / "streamlit_app.py"]
    selected = {}
    for path in paths:
        if path.resolve() == excluded:
            continue
        name = path.relative_to(root).as_posix()
        body = read_file(path)
        _check_content(body, Path(name))
        if name.endswith(".py"):
            compile(body, name, "exec")
        selected[name] = body
    # Rebuild the legacy launcher from tracked code; fresh checkouts do not
    # contain ignored verification artifacts. Only this exact path is allowed.
    selected[TEST_SUPPORT] = (
        '"""Compatibility entry point; implementation is tracked in scripts/."""\n'
        'from pathlib import Path\nimport runpy\n\n'
        '_implementation = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/record_sector_command.py"))\n'
        'globals().update({name: value for name, value in _implementation.items() if not name.startswith("__")})\n\n'
        'if __name__ == "__main__":\n    raise SystemExit(main())\n'
    ).encode("utf-8")
    if DAILY_CONFIG not in selected:
        raise ValueError("观察版配置尚未就绪，不能打包")
    return dict(sorted(selected.items()))


def previous_versions(root, names, baseline):
    versions = {name: set() for name in names}
    base_names = set()

    def remember(name, body):
        if name in versions:
            lf = body.replace(b"\r\n", b"\n")
            versions[name].update(digest(value) for value in (body, lf, lf.replace(b"\n", b"\r\n")))

    for filename in KNOWN_ARCHIVES:
        path = root / "deploy" / filename
        if not path.is_file():
            raise ValueError("缺少已知旧版包：" + filename)
        # Read only allowlisted source members; no archive extraction or data reads.
        with tarfile.open(path, "r:gz") as archive:
            for item in archive.getmembers():
                if not item.isfile() or item.size > 10_000_000:
                    continue
                if item.name.startswith("project/"):
                    name = item.name[len("project/"):]
                    if filename == KNOWN_ARCHIVES[0]:
                        base_names.add(name)
                elif "/files/" in item.name:
                    name = item.name.split("/files/", 1)[1]
                else:
                    continue
                if name in versions:
                    remember(name, archive.extractfile(item).read())
    with zipfile.ZipFile(baseline / "source.zip") as archive:
        protected = json.loads(read_file(baseline / "manifest.json", 2_000_000))
        for name in names:
            if name not in archive.namelist():
                continue
            body = archive.read(name)
            if digest(body) != protected.get(name):
                raise ValueError("修改前源码保护校验不符：" + name)
            remember(name, body)
    return {name: ([None] if name not in base_names else []) + sorted(values) for name, values in versions.items()}


def build(root, baseline, *, output=None, deployment_config=None, project=None, account=None, ssh_host=None):
    settings = load_settings(deployment_config, default_root=root)
    project = resolve_setting(settings, "project_root", project, required=True)
    account = resolve_setting(settings, "account", account)
    ssh_host = resolve_setting(settings, "ssh_host", ssh_host, required=True)
    profile = (Path(deployment_config) if deployment_config is not None else root / ".local/deployment.json").resolve()
    required = [root / "pyproject.toml", root / "streamlit_app.py", root / DAILY_CONFIG,
                *(root / "scripts" / name for name in HELPERS)]
    if any(path.resolve() == profile for path in required):
        raise ValueError("部署配置与升级必需文件冲突；请使用独立部署配置文件")
    bodies = source_files(root, exclude=profile)
    versions = previous_versions(root, bodies, baseline)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = (output or root / "deploy") / ("ashare-observation-upgrade-" + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    entries = []
    for name, body in bodies.items():
        target = directory / "files" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        entries.append({"path": name, "sha256": digest(body), "accepted_previous_sha256": versions[name]})
    for name in HELPERS:
        shutil.copyfile(root / "scripts" / name, directory / name)
    metadata = tomllib.loads(read_file(root / "pyproject.toml").decode("utf-8"))
    dependencies = metadata["project"]["dependencies"] + metadata["project"]["optional-dependencies"]["test"]
    pins = {}
    for value in dependencies:
        match = re.fullmatch(r"([A-Za-z0-9_-]+)==([0-9A-Za-z.+_-]+)", value)
        if not match:
            raise ValueError("依赖必须精确锁定：" + value)
        pins[match[1]] = match[2]
    manifest = {"schema_version": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
                "daily_config": DAILY_CONFIG, "required_versions": pins, "files": entries,
                "test_support_paths": [TEST_SUPPORT], "changes_existing_timer": False,
                "source_baseline": baseline.name}
    if "config/node_runtime.json" in bodies:
        manifest["required_node_runtime"] = json.loads(bodies["config/node_runtime.json"])
    (directory / "upgrade.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    archive_path = directory.with_name(directory.name + ".tar.gz")
    install = ("# Run as " + (account or "the existing project user") + " in the existing SSH session, not with sudo.\ncd ~\n"
        "sha256sum -c " + archive_path.name + ".sha256 &&\n"
        "tar --no-same-owner --no-same-permissions -xzf " + archive_path.name + " &&\n"
        + project + "/.venv/bin/python ./" + directory.name + "/apply_observation_upgrade.py --project " + project + " --apply\n")
    (directory / "INSTALL.txt").write_text(install, encoding="utf-8")
    (directory / "RUN_LATEST.txt").write_text(
        "# After installation succeeds, run once without a fixed historical date.\n"
        "# Before 21:00 Beijing time, not_due is only an early-trigger check, not live collection acceptance.\n"
        "# After the time gate, the trusted calendar checks today. No fallback to yesterday's universe.\n"
        "# Existing model budgets apply.\ncd " + project + " &&\n"
        "./.venv/bin/python -u -m ashare_daily run-daily --config " + DAILY_CONFIG + " --scheduled\n", encoding="utf-8")
    (directory / "VERIFY.txt").write_text(
        "# Read only: the existing daily service, timer and web service.\n"
        "systemctl --user show ashare-daily-research-daily.service -p LoadState -p ActiveState -p Result -p ExecMainStatus -p ExecStart\n"
        "systemctl --user show ashare-daily-research-daily.timer -p ActiveState -p UnitFileState -p LastTriggerUSec -p NextElapseUSecRealtime\n"
        "systemctl --user list-timers --all ashare-daily-research-daily.timer\n"
        "systemctl --user is-active ashare-daily-research-web.service\n", encoding="utf-8")
    (directory / "UPLOAD.ps1").write_text(
        'scp "' + str(archive_path) + '" "' + str(archive_path) + '.sha256" ' + ssh_host + ':~/\n'
        'if ($LASTEXITCODE -ne 0) { throw "Upload failed; do not install." }\n', encoding="utf-8")
    (directory / "README.md").write_text(
        "# Daily observation upgrade\n\n"
        "Updates existing application code and the existing daily service command to the explicit observation configuration. "
        "The existing 21:00 Asia/Shanghai timer file and its enabled state are preserved. No second timer is created.\n\n"
        "INSTALL.txt verifies source hashes, installed dependency versions, the three existing user units and the daily lock; "
        "backs up changed files and the original daily unit; runs the complete offline regression in an independent /tmp; "
        "then updates the daily command and restarts an already-active web service. Failure rolls back changed files and the daily unit. "
        "Unknown server modifications or unit drop-ins stop installation. No credentials, database, historical report or budget is copied.\n\n"
        "The package includes the exact non-data test helper outputs/verification/f2s1/run_command.py. "
        "No source acquisition or model call occurs during installation. RUN_LATEST.txt is a separate, budgeted live verification. "
        "Read the generated result.json and pytest.txt under outputs/verification/linux/observation-upgrades/. "
        "A source-only before/ and rollback.json plus daily.service.before record the rollback boundary.\n", encoding="utf-8")
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=directory.name + "/" + path.relative_to(directory).as_posix())
    checksum = digest(archive_path.read_bytes())
    archive_path.with_name(archive_path.name + ".sha256").write_text(checksum + "  " + archive_path.name + "\n", encoding="ascii")
    return {"directory": str(directory), "archive": str(archive_path), "sha256": checksum, "files": len(entries)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--project", help="Linux目标项目绝对路径")
    parser.add_argument("--account", help="现有项目普通用户")
    parser.add_argument("--ssh-host", help="已配置的SSH主机别名或主机名")
    add_deployment_argument(parser)
    args = parser.parse_args(argv)
    print(json.dumps(build(ROOT, args.baseline.absolute(), deployment_config=args.deployment_config,
                           project=args.project, account=args.account, ssh_host=args.ssh_host), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
