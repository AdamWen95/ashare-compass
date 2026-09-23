"""Portable simulation of Windows rename errors; no original backup is altered."""
from pathlib import Path

import pytest

from ashare_daily.operations import backup


def winerror(number):
    error = PermissionError("simulated Windows directory sharing condition")
    error.winerror = number
    return error


@pytest.fixture
def directories(tmp_path):
    stage, target = tmp_path/"stage", tmp_path/"restored"
    stage.mkdir()
    (stage/"frozen.txt").write_bytes(b"OFFLINE immutable fixture")
    return stage, target


@pytest.mark.parametrize("number", [5, 32, 33])
def test_known_windows_sharing_error_retries_then_publishes_once(directories, monkeypatch, number):
    stage, target = directories
    original = Path.rename
    calls, sleeps = [], []

    def transient(path, destination):
        calls.append((path, destination))
        if len(calls) < 3:
            raise winerror(number)
        return original(path, destination)

    monkeypatch.setattr(Path, "rename", transient)
    monkeypatch.setattr(backup.time, "sleep", sleeps.append)
    backup._publish_restored_stage(stage, target)
    assert len(calls) == 3 and sleeps == [.05, .1]
    assert not stage.exists() and (target/"frozen.txt").read_bytes() == b"OFFLINE immutable fixture"


def test_permanent_windows_access_denial_is_bounded_and_retains_stage(directories, monkeypatch):
    stage, target = directories
    calls, sleeps = [], []

    def denied(path, destination):
        calls.append(1)
        raise winerror(5)

    monkeypatch.setattr(Path, "rename", denied)
    monkeypatch.setattr(backup.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        backup._publish_restored_stage(stage, target)
    assert len(calls) == 4 and sleeps == [.05, .1, .2]
    assert stage.is_dir() and not target.exists()
    assert (stage/"frozen.txt").read_bytes() == b"OFFLINE immutable fixture"


@pytest.mark.parametrize("number", [None, 2, 183])
def test_posix_permission_or_unrelated_windows_error_never_retries(directories, monkeypatch, number):
    stage, target = directories
    calls, sleeps = [], []

    def denied(path, destination):
        calls.append(1)
        if number is None:
            raise PermissionError(13, "ordinary permission denied")
        raise winerror(number)

    monkeypatch.setattr(Path, "rename", denied)
    monkeypatch.setattr(backup.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        backup._publish_restored_stage(stage, target)
    assert len(calls) == 1 and not sleeps
    assert stage.exists() and not target.exists()


def test_destination_appearing_between_attempts_is_never_overwritten(directories, monkeypatch):
    stage, target = directories
    calls = []

    def fail_once(path, destination):
        calls.append(1)
        raise winerror(32)

    def concurrent_creator(seconds):
        target.mkdir()
        (target/"someone_else.txt").write_bytes(b"must remain")

    monkeypatch.setattr(Path, "rename", fail_once)
    monkeypatch.setattr(backup.time, "sleep", concurrent_creator)
    with pytest.raises(ValueError, match="未覆盖"):
        backup._publish_restored_stage(stage, target)
    assert len(calls) == 1 and stage.exists()
    assert (target/"someone_else.txt").read_bytes() == b"must remain"


def test_preexisting_destination_does_not_attempt_rename(directories, monkeypatch):
    stage, target = directories
    target.mkdir()
    monkeypatch.setattr(Path, "rename", lambda *args: pytest.fail("existing destination must not be renamed over"))
    with pytest.raises(ValueError, match="未覆盖"):
        backup._publish_restored_stage(stage, target)
    assert stage.exists() and target.exists()
