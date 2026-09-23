"""Record one explicitly requested F2-S1 CLI invocation without reading prices.

Example: python -B scripts/record_sector_command.py -- sector prepare
  --config config/sector_first.json --selection sector-YYYY-MM-DD-HASH
  --max-seconds 1200

The wrapper has no network implementation. Its validated CLI child may perform
the caller-authorized operation; use the separately approved execution context.
It never schedules, retries, enables a source, reads credentials or calls a model.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo("Asia/Shanghai")
DATABASES = ("data/research/market.sqlite3", "data/research/universe.sqlite3",
             "data/research/f2_jobs.sqlite3", "data/operations/runtime.sqlite3")


def now():
    return datetime.now(ZONE).isoformat()


def io_path(path):
    value = str(Path(path).absolute())
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def hash_file(path):
    result = hashlib.sha256()
    with io_path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(part)
    return result.hexdigest()


def save_new(path, value):
    io_path(path.parent).mkdir(parents=True, exist_ok=True)
    with io_path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def safe_project_path(value):
    path = ROOT / value
    if any(part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()) for part in (path, *path.parents)):
        raise ValueError("path cannot traverse a symlink or junction")
    path = path.resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("path must remain inside the project")
    return path


def validate_cli(arguments):
    args = list(arguments)
    if not args:
        raise ValueError("one explicit CLI command is required")
    sector = len(args) >= 2 and args[0] == "sector" and args[1] in {"prepare", "resume", "report"}
    daily = args[0] == "run-daily"
    if not sector and not daily:
        raise ValueError("only sector prepare/resume/report or run-daily is allowed")
    remaining = args[2:] if sector else args[1:]
    valued = {"--config", "--selection", "--max-seconds"} if sector else {"--config", "--date", "--cutoff", "--start", "--market-max-seconds"}
    switches = {"--dry-run"} if sector else {"--dry-run", "--skip-model"}
    values = {}
    index = 0
    while index < len(remaining):
        flag = remaining[index]
        if flag in values or flag not in valued | switches:
            raise ValueError("unknown or repeated CLI flag: " + flag)
        if flag in switches:
            values[flag] = True
            index += 1
        else:
            if index + 1 == len(remaining) or remaining[index + 1].startswith("--"):
                raise ValueError("missing CLI flag value: " + flag)
            values[flag] = remaining[index + 1]
            index += 2
    expected_config = "config/sector_first.json" if sector else "config/sector_first_daily.json"
    if values.get("--config", expected_config) != expected_config:
        raise ValueError("only the explicit F2-S1 configuration is allowed")
    if "--config" not in values:
        args.extend(["--config", expected_config])
    if sector and not re.fullmatch(r"sector-\d{4}-\d{2}-\d{2}-[a-f0-9]{20}", values.get("--selection", "")):
        raise ValueError("an exact frozen selection ID is required")
    if daily:
        target = values.get("--date", "")
        if date.fromisoformat(target).isoformat() != target:
            raise ValueError("an exact daily target date is required")
        if not values.get("--dry-run") and not values.get("--skip-model"):
            raise ValueError("run-daily requires --skip-model")
    for key in ("--max-seconds", "--market-max-seconds"):
        if key in values and (not str(values[key]).isdigit() or not 1 <= int(values[key]) <= 14400):
            raise ValueError("CLI runtime must be an integer from 1 to 14400 seconds")
    for key in ("--cutoff", "--start"):
        if key in values and datetime.fromisoformat(values[key]).utcoffset() is None:
            raise ValueError("explicit timezone is required")
    # Read only the known, noncredential F2-S1 routing configuration. Do not open
    # any data/response file or .env, and do not inspect database contents.
    config = json.loads(safe_project_path(expected_config).read_text(encoding="utf-8-sig"))
    if config.get("research_mode") != "sector_first":
        raise ValueError("configuration is not sector_first")
    if sector:
        if config.get("model_calls") != 0 or config.get("market_scope") != "sse_szse_a":
            raise ValueError("configuration scope/model boundary differs")
        output = safe_project_path(config["output_directory"])
    else:
        if config.get("sector_config") != "config/sector_first.json" or config.get("market_config") is not None:
            raise ValueError("daily configuration is not the isolated sector route")
        sector_config = json.loads(safe_project_path("config/sector_first.json").read_text(encoding="utf-8-sig"))
        if sector_config.get("model_calls") != 0:
            raise ValueError("model calls must remain zero")
        output = safe_project_path(sector_config["output_directory"])
    return args, output, values.get("--selection")


def source_snapshot():
    paths = [path for directory in (ROOT / "src", ROOT / "tests") for path in directory.rglob("*.py") if "__pycache__" not in path.parts]
    paths.extend([ROOT / "pyproject.toml", Path(__file__).resolve()])
    return {str(path.relative_to(ROOT)).replace("\\", "/"): {"bytes": path.stat().st_size, "sha256": hash_file(path)}
            for path in sorted(set(paths)) if path.is_file()}


def database_sizes():
    values = {}
    for relative in DATABASES:
        path = safe_project_path(relative)
        values[relative] = {"exists": path.is_file(), "bytes": path.stat().st_size if path.is_file() else None,
            "sidecars": {suffix: (Path(str(path) + suffix).stat().st_size if Path(str(path) + suffix).is_file() else None)
                         for suffix in ("-wal", "-shm", "-journal")}}
    return values


def checkpoint_counts(output, selection):
    paths = [output / selection] if selection else list(output.glob("sector-*"))
    files, securities = 0, 0
    for path in paths:
        directory = io_path(path / "history/checkpoints")
        if not directory.is_dir():
            continue
        for task in directory.iterdir():
            if task.is_dir():
                count = sum(1 for _ in task.glob("*.json"))
                files += count
                securities += int(count > 0)
    return {"checkpoint_files": files, "securities_with_checkpoints": securities}


def stop_owned_child(process):
    """Stop only this invocation's process tree after an explicit interruption."""
    if process.poll() is not None:
        return "already_exited"
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=20)
        return "interrupt_delivered"
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "nt":
            executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"
            subprocess.run([str(executable), "/PID", str(process.pid), "/T", "/F"], check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=15)
        return "owned_child_tree_terminated_after_interrupt"


def child_command(interpreter, arguments):
    # __main__.py invokes cli.main(); importing ashare_daily.cli alone does not.
    return [str(interpreter), "-B", "-X", "utf8", "-m", "ashare_daily", *arguments]


def project_interpreter():
    """Accept a venv's interpreter link without weakening data path validation.

    Linux venvs normally link their executable to the base Python. Calling that
    resolved target directly would discard the venv, so retain the lexical venv
    path and verify this wrapper is already running in that exact environment.
    """
    environment = safe_project_path(".venv")
    directory = safe_project_path(".venv/Scripts" if os.name == "nt" else ".venv/bin")
    interpreter = directory / ("python.exe" if os.name == "nt" else "python")
    if not interpreter.is_file():
        raise ValueError("project virtual environment interpreter is missing")
    if Path(sys.prefix).resolve() != environment or not os.path.samefile(sys.executable, interpreter):
        raise ValueError("wrapper must run with this project's virtual environment")
    return interpreter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="Validate the command; do not run or write artifacts")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args()
    args = parsed.command[1:] if parsed.command[:1] == ["--"] else parsed.command
    try:
        args, checkpoint_root, selection = validate_cli(args)
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    if parsed.check_only:
        print(json.dumps({"status": "validated_not_executed", "cli_args": args,
            "child_started": False, "network_requests": 0, "database_writes": 0}, ensure_ascii=False))
        return 0
    try:
        interpreter = project_interpreter()
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    command = child_command(interpreter, args)
    run = ROOT / "outputs/verification/f2s1/command-runs" / (datetime.now(ZONE).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid.uuid4().hex[:8])
    before_sources, before_sizes = source_snapshot(), database_sizes()
    before_counts = checkpoint_counts(checkpoint_root, selection)
    save_new(run / "source-before.json", before_sources)
    save_new(run / "databases-before.json", before_sizes)
    save_new(run / "manifest.json", {"schema_version": "f2s1-recorded-command-v1", "started_at": now(),
        "command": command, "cwd": str(ROOT), "script_sha256": hash_file(Path(__file__)), "selection_id": selection,
        "progress_kind": "checkpoint_file_counts_only", "price_files_read": 0, "credential_files_read": 0,
        "wrapper_network_requests": 0, "child_operation_requires_caller_authorization": True})
    stdout_path, stderr_path = run / "stdout.txt", run / "stderr.txt"
    started, child, interrupt, run_error = time.monotonic(), None, None, None
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP, "startupinfo": subprocess.STARTUPINFO()} if os.name == "nt" else {"start_new_session": True}
    if os.name == "nt":
        options["startupinfo"].dwFlags |= subprocess.STARTF_USESHOWWINDOW
        options["startupinfo"].wShowWindow = subprocess.SW_HIDE
    print(json.dumps({"record_directory": str(run), **before_counts}, ensure_ascii=False), flush=True)
    with io_path(stdout_path).open("xb") as stdout, io_path(stderr_path).open("xb") as stderr:
        try:
            child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **options)
            while True:
                try:
                    child.wait(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    print(json.dumps(checkpoint_counts(checkpoint_root, selection)), flush=True)
        except KeyboardInterrupt:
            interrupt = stop_owned_child(child) if child is not None else "interrupted_before_child"
        except (OSError, ValueError) as exc:
            run_error = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            if child is not None and child.poll() is None:
                interrupt = stop_owned_child(child)
    after_sources, after_sizes = source_snapshot(), database_sizes()
    save_new(run / "source-after.json", after_sources)
    save_new(run / "databases-after.json", after_sizes)
    changed = [key for key in sorted(set(before_sources) | set(after_sources)) if before_sources.get(key) != after_sources.get(key)]
    code = child.returncode if child is not None else None
    stdout_bytes = io_path(stdout_path).stat().st_size
    empty_success = code == 0 and stdout_bytes == 0
    wrapper_exit = 130 if interrupt else 3 if empty_success or run_error else code if isinstance(code, int) and 0 <= code <= 255 else 2
    result = {"schema_version": "f2s1-recorded-command-result-v1", "completed_at": now(), "command": command,
        "child_exit_code": code, "wrapper_exit_code": wrapper_exit,
        "cli_entrypoint": "ashare_daily.__main__", "cli_output_present": stdout_bytes > 0,
        "invalid_empty_success": empty_success, "run_error": run_error,
        "elapsed_seconds": round(time.monotonic() - started, 3), "interrupt": interrupt,
        "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
        "stdout_bytes": stdout_bytes, "stderr_bytes": io_path(stderr_path).stat().st_size,
        "database_sizes_before": before_sizes, "database_sizes_after": after_sizes,
        "source_files_changed": changed, "source_files_unchanged": not changed,
        "checkpoint_counts_before": before_counts, "checkpoint_counts_after": checkpoint_counts(checkpoint_root, selection),
        "wrapper_network_requests": 0, "price_files_read": 0, "credential_files_read": 0,
        "child_running_after_return": child is not None and child.poll() is None}
    save_new(run / "result.json", result)
    print(json.dumps({"record_directory": str(run), "child_exit_code": code,
        "elapsed_seconds": result["elapsed_seconds"], "source_files_unchanged": not changed,
        **result["checkpoint_counts_after"]}, ensure_ascii=False), flush=True)
    return wrapper_exit


if __name__ == "__main__":
    raise SystemExit(main())
