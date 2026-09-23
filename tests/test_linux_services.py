"""Offline service-generation checks: no systemctl/SSH/provider is invoked."""
from datetime import datetime
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts/linux_services.py"
spec = importlib.util.spec_from_file_location("linux_services", PATH)
services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(services)
PROJECT = "/home/researcher/apps/ashare-daily-research"


def plan():
    return services.make_plan(PROJECT, "researcher", now=datetime.fromisoformat("2026-09-10T12:00:00+00:00"), offline=True)


class OfflineRunner:
    def __init__(self, *, daily_running=False, fail_verify=False):
        self.calls = []
        self.daily_running = daily_running
        self.fail_verify = fail_verify

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[:3] == ["systemd-analyze", "--user", "verify"] and self.fail_verify:
            raise RuntimeError("OFFLINE invalid unit")
        output = "OFFLINE TEST"
        if "--property=ActiveState" in command:
            output = "activating" if self.daily_running else "inactive"
        return {"command": command, "exit_code": 0, "stdout": output, "stderr": ""}


def installed(tmp_path):
    generated = plan()
    runner = OfflineRunner()
    result = services.manage("install", generated, tmp_path, confirmed=True, runner=runner)
    assert len(result["changed_units"]) == 3
    return generated


def test_fixed_beijing_daily_and_local_preview():
    p = plan()
    assert p["next_beijing"] == "2026-09-10T21:00:00+08:00"
    assert p["next_local"] == "2026-09-10T13:00:00+00:00"
    timer = p["units"][services.TIMER]
    assert "OnCalendar=*-*-* 21:00:00 Asia/Shanghai" in timer
    assert "Persistent=true" in timer
    assert "WakeSystem=false" in timer
    assert "RandomizedDelaySec=0" in timer
    assert "Unit=" + services.DAILY in timer


def test_daily_uses_absolute_venv_and_preserves_partial_and_lock_codes():
    daily = plan()["units"][services.DAILY]
    assert "WorkingDirectory=" + PROJECT in daily
    assert "ExecStart=" + PROJECT + "/.venv/bin/python -m ashare_daily run-daily --scheduled" in daily
    assert "Type=oneshot" in daily
    assert "SuccessExitStatus=1 3" in daily
    assert "Restart=no" in daily
    assert "RemainAfterExit" not in daily
    assert "User=" not in daily


def test_web_listens_only_loopback_and_cannot_generate_research():
    web = plan()["units"][services.WEB]
    assert "--server.address 127.0.0.1 --server.port 8501" in web
    assert "streamlit_app.py" in web
    assert "run-daily" not in web
    assert "--browser.gatherUsageStats false" in web
    assert "UMask=0077" in web
    assert "NoNewPrivileges=true" in web


def test_units_do_not_read_or_embed_key(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "sk-OFFLINE-TEST-NOT-FOR-UNITS")
    text = json.dumps(plan())
    assert "sk-OFFLINE" not in text
    assert "Environment=" not in text
    assert "EnvironmentFile=" not in text


@pytest.mark.parametrize("path", ["/", "/home/a/../b", "/home/a/my project", "/home/a/$PROJECT", "/home/a/%n", "/home/a/test\nExecStart=evil", "D:\\project", "/home/a/./project"])
def test_unit_path_injection_is_rejected(path):
    with pytest.raises(ValueError):
        services.build_units(path)


def test_preview_is_pure_and_does_not_write_or_run(tmp_path):
    runner = OfflineRunner()
    assert services.manage("preview", plan(), tmp_path / "absent", runner=runner)["operation"] == "preview"
    assert runner.calls == []
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("action", ["install", "enable", "start-web", "run-daily"])
def test_long_running_or_external_actions_require_explicit_confirmation(tmp_path, action):
    runner = OfflineRunner()
    with pytest.raises(ValueError, match="confirm"):
        services.manage(action, plan(), tmp_path, runner=runner)
    assert runner.calls == []
    assert list(tmp_path.iterdir()) == []


def test_install_validates_and_repeated_install_is_unchanged_without_enable(tmp_path):
    generated = installed(tmp_path)
    runner = OfflineRunner()
    result = services.manage("install", generated, tmp_path, confirmed=True, runner=runner)
    assert result["changed_units"] == []
    assert runner.calls[0][:3] == ["systemd-analyze", "--user", "verify"]
    assert runner.calls[-1] == ["systemctl", "--user", "daemon-reload"]
    assert not any("enable" in c or "start" in c for c in runner.calls)


def test_bad_generated_unit_does_not_replace_installed_content(tmp_path):
    generated = installed(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(RuntimeError):
        services.manage("install", generated, tmp_path, confirmed=True, runner=OfflineRunner(fail_verify=True))
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_foreign_same_name_is_never_overwritten_or_started(tmp_path):
    path = tmp_path / services.DAILY
    path.write_text("# another project\n[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    for action in ("install", "enable", "disable", "uninstall"):
        runner = OfflineRunner()
        with pytest.raises(ValueError, match="不属于"):
            services.manage(action, plan(), tmp_path, confirmed=True, runner=runner)
        assert runner.calls == []
    assert path.read_text(encoding="utf-8").startswith("# another project")


def test_drop_in_must_be_reviewed_before_service_management(tmp_path):
    generated = installed(tmp_path)
    (tmp_path / (services.DAILY + ".d")).mkdir()
    with pytest.raises(ValueError, match="drop-in"):
        services.manage("enable", generated, tmp_path, confirmed=True, runner=OfflineRunner())


def test_orphan_drop_in_is_rejected_before_first_install(tmp_path):
    (tmp_path / (services.DAILY + ".d")).mkdir()
    runner = OfflineRunner()
    with pytest.raises(ValueError, match="drop-in"):
        services.manage("install", plan(), tmp_path, confirmed=True, runner=runner)
    assert runner.calls == []


def test_enable_is_explicit_and_only_manages_project_timer_and_web(tmp_path):
    generated = installed(tmp_path)
    runner = OfflineRunner()
    services.manage("enable", generated, tmp_path, confirmed=True, runner=runner)
    assert runner.calls == [["systemctl", "--user", "enable", "--now", services.TIMER, services.WEB]]


def test_disable_does_not_kill_active_daily_job(tmp_path):
    generated = installed(tmp_path)
    runner = OfflineRunner()
    services.manage("disable", generated, tmp_path, runner=runner)
    assert runner.calls == [["systemctl", "--user", "disable", "--now", services.TIMER, services.WEB]]


def test_uninstall_removes_only_registered_units_not_user_data(tmp_path):
    generated = installed(tmp_path)
    keep = tmp_path / "unrelated.service"
    keep.write_text("retain", encoding="utf-8")
    services.manage("uninstall", generated, tmp_path, runner=OfflineRunner())
    assert keep.read_text(encoding="utf-8") == "retain"
    assert set(p.name for p in tmp_path.iterdir()) == {"unrelated.service"}


def test_uninstall_refuses_to_interrupt_running_research(tmp_path):
    generated = installed(tmp_path)
    with pytest.raises(ValueError, match="仍在运行"):
        services.manage("uninstall", generated, tmp_path, runner=OfflineRunner(daily_running=True))
    assert set(p.name for p in tmp_path.iterdir()) == set(services.UNITS)


def test_local_offline_preview_cli_does_not_touch_systemd(tmp_path):
    output = tmp_path / "linux-preview.json"
    config = tmp_path / "deployment.json"
    config.write_text('{}', encoding="utf-8")
    code = services.main(["preview", "--render-only", "--project-root", PROJECT,
                          "--account", "researcher", "--deployment-config", str(config), "--output", str(output)])
    assert code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["verification_kind"] == "offline_render"


def test_render_only_cannot_be_used_to_install(tmp_path):
    config = tmp_path / "deployment.json"
    config.write_text('{}', encoding="utf-8")
    assert services.main(["install", "--render-only", "--project-root", PROJECT,
                          "--account", "researcher", "--deployment-config", str(config), "--confirm"]) == 2


def test_cli_uses_config_account_but_keeps_explicit_project_local(tmp_path, capsys):
    config = tmp_path / "deployment.json"
    config.write_text(json.dumps({"account": "researcher", "project_root": "/home/remote/apps/research"}), encoding="utf-8")
    assert services.main(["preview", "--render-only", "--project-root", PROJECT,
                          "--deployment-config", str(config)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["account"] == "researcher"
    assert output["project_root"] == PROJECT


def test_cli_explicit_account_overrides_config(tmp_path, capsys):
    config = tmp_path / "deployment.json"
    config.write_text('{"account": "configured"}', encoding="utf-8")
    assert services.main(["preview", "--render-only", "--project-root", PROJECT,
                          "--account", "researcher", "--deployment-config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)["account"] == "researcher"


def test_cli_default_project_uses_actual_script_root_not_remote_config(tmp_path, monkeypatch, capsys):
    config = tmp_path / "deployment.json"
    config.write_text(json.dumps({"account": "researcher", "project_root": "/home/remote/apps/research"}), encoding="utf-8")
    # A Windows checkout cannot pass the Linux path syntax check. Keep target
    # selection real while allowing its unit rendering on either test platform.
    monkeypatch.setattr(services, "checked_linux_path", lambda value: PROJECT)
    assert services.main(["preview", "--render-only", "--deployment-config", str(config)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["project_root"] == str(PATH.resolve().parents[1])
    assert output["account"] == "researcher"


@pytest.mark.parametrize("current_account,expected_code", [("researcher", 0), ("root", 2)])
def test_cli_without_account_resolves_only_current_nonroot_user(tmp_path, monkeypatch, capsys, current_account, expected_code):
    config = tmp_path / "deployment.json"
    config.write_text('{}', encoding="utf-8")
    monkeypatch.setattr(services.getpass, "getuser", lambda: current_account)
    assert services.main(["preview", "--render-only", "--project-root", PROJECT,
                          "--deployment-config", str(config)]) == expected_code
    output = capsys.readouterr().out
    if expected_code == 0:
        assert json.loads(output)["account"] == "researcher"
    else:
        assert not output


def test_cli_missing_config_fails_before_service_changes(tmp_path, capsys):
    assert services.main(["preview", "--render-only", "--project-root", PROJECT,
                          "--account", "researcher", "--deployment-config", str(tmp_path / "missing.json")]) == 2
    assert not capsys.readouterr().out
