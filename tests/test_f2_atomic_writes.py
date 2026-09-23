"""Retain atomic-write guarantees under observed Windows sharing failures."""
import json

import pytest

from ashare_daily.operations import daily


def denied():
    error = PermissionError("synthetic Windows sharing denial")
    error.winerror = 5
    return error


def test_transient_windows_replace_retries_without_partial_target(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text('{"old":true}', encoding="utf-8")
    replace, calls = daily.os.replace, []

    def transient(source, destination):
        calls.append(1)
        if len(calls) == 1:
            assert json.loads(target.read_text()) == {"old": True}
            raise denied()
        return replace(source, destination)

    monkeypatch.setattr(daily.os, "replace", transient)
    daily.atomic_json(target, {"new": True})
    assert len(calls) == 2 and json.loads(target.read_text()) == {"new": True}
    assert list(tmp_path.glob("*.tmp-*")) == []


def test_persistent_denial_is_bounded_and_preserves_original(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text('{"old":true}', encoding="utf-8")
    calls = []

    def permanent(*args):
        calls.append(1)
        raise denied()

    monkeypatch.setattr(daily.os, "replace", permanent)
    with pytest.raises(PermissionError):
        daily.atomic_json(target, {"new": True})
    assert len(calls) == 4 and json.loads(target.read_text()) == {"old": True}
    assert len(list(tmp_path.glob("*.tmp-*"))) == 1
