"""Deployment settings stay local and are inert until explicitly consumed."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "tested_deployment_settings", Path(__file__).resolve().parents[1] / "scripts/deployment_settings.py"
)
settings = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(settings)


def write_config(root, values, name=".local/deployment.json"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_default_config_is_repo_relative_and_missing_is_inert(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert settings.load_settings(default_root=tmp_path) == {}
    write_config(tmp_path, {"account": "researcher", "listen_port": 8502})
    assert settings.load_settings(default_root=tmp_path)["account"] == "researcher"


def test_explicit_config_wins_over_default_and_relative_path_is_cwd_relative(tmp_path, monkeypatch):
    write_config(tmp_path, {"account": "default-user"})
    write_config(tmp_path, {"account": "selected-user"}, "selected.json")
    monkeypatch.chdir(tmp_path)
    assert settings.load_settings(Path("selected.json"), default_root=tmp_path) == {"account": "selected-user"}


def test_explicit_missing_file_does_not_silently_use_another_target(tmp_path):
    write_config(tmp_path, {"account": "default-user"})
    with pytest.raises(ValueError, match="部署配置"):
        settings.load_settings(tmp_path / "missing.json", default_root=tmp_path)


def test_cli_override_required_values_and_unknown_fields(tmp_path):
    source = {"project_root": "/home/researcher/apps/report", "server_ip": "192.168.56.10", "listen_port": 8502}
    path = write_config(tmp_path, source)
    loaded = settings.load_settings(path)
    assert settings.resolve_setting(loaded, "listen_port", 8602, required=True) == 8602
    assert settings.resolve_setting(loaded, "project_root", required=True) == source["project_root"]
    assert settings.resolve_setting({}, "ssh_host") is None
    with pytest.raises(ValueError, match="ssh_host"):
        settings.resolve_setting({}, "ssh_host", required=True)
    with pytest.raises(ValueError):
        settings.resolve_setting(loaded, "listen_port", "")


@pytest.mark.parametrize("name,value", [
    ("project_root", "/"), ("project_root", "relative/project"),
    ("project_root", "/home/researcher/../other"),
    ("project_root", "/home/researcher/$(command)"),
    ("project_root", "/home/researcher/a\ncommand"),
    ("account", "root;command"), ("account", "user name"),
    ("ssh_host", "-oProxyCommand=command"), ("ssh_host", "host:/tmp"),
    ("ssh_host", "host\ncommand"), ("ssh_host", "user@host"),
    ("server_ip", "0.0.0.0"), ("server_ip", "127.0.0.1"),
    ("server_ip", "8.8.8.8"), ("server_ip", "192.168.56.0/24"),
    ("listen_port", 0), ("listen_port", 65536),
    ("listen_port", True), ("listen_port", "8502"),
])
def test_invalid_values_fail_without_echoing_the_input(tmp_path, name, value):
    path = write_config(tmp_path, {name: value})
    with pytest.raises(ValueError) as caught:
        settings.load_settings(path)
    assert name in str(caught.value)
    assert repr(value) not in str(caught.value)


@pytest.mark.parametrize("values", [[], {"password": "PRIVATE_TEST_VALUE"},
                                     {"schema_version": "future-v9"}, {"account": None}])
def test_unsupported_shapes_versions_and_credentials_are_rejected(tmp_path, values):
    path = write_config(tmp_path, values)
    with pytest.raises(ValueError) as caught:
        settings.load_settings(path)
    assert "PRIVATE_TEST_VALUE" not in str(caught.value)


def test_invalid_or_oversized_json_is_not_echoed(tmp_path):
    path = tmp_path / "invalid.json"
    for content in [b'{"password":PRIVATE_TEST_VALUE}', b' ' * 65537, b'\xff']:
        path.write_bytes(content)
        with pytest.raises(ValueError) as caught:
            settings.load_settings(path)
        assert "PRIVATE_TEST_VALUE" not in str(caught.value)


def test_cli_config_flag_and_complete_example(tmp_path):
    parser = argparse.ArgumentParser()
    settings.add_deployment_argument(parser)
    path = write_config(tmp_path, {
        "schema_version": "deployment-local-v1", "project_root": "/home/researcher/apps/report",
        "account": "researcher", "ssh_host": "research-host", "server_ip": "192.168.56.10", "listen_port": 8502,
    })
    args = parser.parse_args(["--deployment-config", str(path)])
    loaded = settings.load_settings(args.deployment_config)
    assert loaded["ssh_host"] == "research-host"
    assert parser.parse_args([]).deployment_config is None
