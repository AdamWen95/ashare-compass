"""Manual F4-S1 CLI contracts; all dispatched work below is an offline double."""
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

from ashare_daily.cli import main
from ashare_daily.operations.lock import AlreadyRunning, ProcessLock


OPERATIONS = ("f4s1", "f4s1-resume", "f4s1-report", "gaps", "qualify")


def arguments(operation):
    args = ["sector", operation, "--selection", "validation-frozen-id"]
    if operation == "f4s1":
        args += ["--gap-diagnosis", "proof/gaps.json", "--eligibility-evidence", "proof/fields.json"]
    elif operation in {"f4s1-resume", "f4s1-report"}:
        args += ["--revision", "f4s1-frozen-revision"]
    return args


def install_dispatch(monkeypatch, tmp_path, function):
    module = ModuleType("ashare_daily.sector_f4s1")
    module.run_f4s1 = function
    monkeypatch.setitem(sys.modules, "ashare_daily.sector_f4s1", module)
    monkeypatch.setattr("ashare_daily.operations.daily.PROJECT", tmp_path)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_f4s1_manual_dispatch_defaults_and_existing_daily_lock(monkeypatch, tmp_path, capsys, operation):
    calls = []
    def dispatch(root, args):
        calls.append(args)
        assert root == tmp_path
        with pytest.raises(AlreadyRunning):
            with ProcessLock(tmp_path/"data/operations/daily.lock", "nested-probe"):
                pytest.fail("F4-S1 dispatch did not hold the shared OS lock")
        return {"status": "offline_dispatch_probe", "model_calls": 0}, 2
    install_dispatch(monkeypatch, tmp_path, dispatch)
    assert main(arguments(operation)) == 2
    assert len(calls) == 1
    args = calls[0]
    assert args.sector_command == operation and args.selection == "validation-frozen-id"
    assert args.validation_config == "config/sector_validation.json"
    assert args.strategy_config == "config/sector_screening_f4s1.json"
    assert args.max_seconds == 120 and not args.dry_run
    assert not getattr(args, "online", False)
    output = json.loads(capsys.readouterr().out)
    assert output["exit_code"] == 2 and output["model_calls"] == 0


@pytest.mark.parametrize("operation", OPERATIONS)
def test_f4s1_dry_run_does_not_create_lock_or_enable_online(monkeypatch, tmp_path, operation):
    def dispatch(root, args):
        assert args.dry_run and not (root/"data").exists()
        assert not getattr(args, "online", False)
        return {"status": "dry_run", "network_requests": 0, "model_calls": 0}, 0
    install_dispatch(monkeypatch, tmp_path, dispatch)
    assert main(arguments(operation)+["--dry-run"]) == 0
    assert not list(tmp_path.iterdir())


def test_existing_lock_prevents_qualification_dispatch(monkeypatch, tmp_path, capsys):
    def forbidden(*args):
        pytest.fail("duplicate F4-S1 dispatch executed")
    install_dispatch(monkeypatch, tmp_path, forbidden)
    with ProcessLock(tmp_path/"data/operations/daily.lock", "existing-daily"):
        assert main(arguments("qualify")) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "already_running"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_every_new_command_requires_frozen_selection(operation):
    with pytest.raises(SystemExit) as result:
        main(["sector", operation])
    assert result.value.code == 2


@pytest.mark.parametrize("missing", ["--gap-diagnosis", "--eligibility-evidence"])
def test_new_revision_requires_both_existing_input_artifacts(missing):
    args = arguments("f4s1")
    index = args.index(missing)
    del args[index:index+2]
    with pytest.raises(SystemExit) as result:
        main(args)
    assert result.value.code == 2


@pytest.mark.parametrize("operation", ["f4s1-resume", "f4s1-report"])
def test_resume_and_report_require_original_revision(operation):
    with pytest.raises(SystemExit) as result:
        main(["sector", operation, "--selection", "validation-frozen-id"])
    assert result.value.code == 2


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "14401"])
def test_runtime_limit_uses_existing_positive_bounded_seconds(value):
    with pytest.raises(SystemExit) as result:
        main(arguments("qualify")+["--max-seconds", value])
    assert result.value.code == 2


@pytest.mark.parametrize("operation", ["gaps", "qualify"])
def test_online_is_explicit_and_source_inputs_are_passed_without_dispatching_other_routes(monkeypatch, tmp_path, operation):
    calls = []
    def dispatch(root, args):
        calls.append(args)
        return {"status": "argument_probe", "model_calls": 0}, 0
    install_dispatch(monkeypatch, tmp_path, dispatch)
    extra = ["--source-revision", "original-gap-version"] if operation == "gaps" else ["--eligibility-evidence", "proof/fields.json"]
    assert main(arguments(operation)+["--online", "--max-seconds", "35", *extra]) == 0
    assert len(calls) == 1 and calls[0].online is True and calls[0].max_seconds == 35
    if operation == "gaps":
        assert calls[0].source_revision == "original-gap-version"
    else:
        assert calls[0].eligibility_evidence == Path("proof/fields.json")


@pytest.mark.parametrize("operation", ["f4s1", "f4s1-resume", "f4s1-report"])
def test_replay_and_preparation_do_not_accept_online_switch(operation):
    with pytest.raises(SystemExit) as result:
        main(arguments(operation)+["--online"])
    assert result.value.code == 2


def test_existing_f3_screen_defaults_are_unchanged(monkeypatch, tmp_path):
    calls = []
    def dispatch(root, args):
        calls.append(args)
        return {"status": "dry_run"}, 0
    monkeypatch.setattr("ashare_daily.sector_workflow.run_f3s", dispatch)
    monkeypatch.setattr("ashare_daily.operations.daily.PROJECT", tmp_path)
    assert main(["sector", "screen", "--selection", "old-frozen-id", "--dry-run"]) == 0
    assert calls[0].strategy_config == "config/sector_screening.json" and calls[0].max_seconds == 600
