"""Offline upgrade/rollback checks; never stop a service or contact a server."""
import importlib.util
import json
from pathlib import Path
import sys
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('offline_research_upgrade', ROOT / 'scripts/apply_research_upgrade.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)
sys.path.pop(0)


@pytest.fixture
def fixture(tmp_path):
    project, package = tmp_path / 'OFFLINE-project', tmp_path / 'package'
    (project / 'src/ashare_daily').mkdir(parents=True)
    (project / 'config').mkdir()
    (project / 'config/m4.json').write_text('{}', encoding='utf-8')
    originals = {'src/ashare_daily/one.py': b'# old\n', 'config/m3_daily.json': None}
    entries = []
    for name, old in originals.items():
        body = b'# new\n' if name.endswith('.py') else b'{}\n'
        if old:
            (project / name).write_bytes(old)
        path = package / 'files' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        entries.append({'path': name, 'sha256': upgrade.digest(body),
                        'accepted_previous_sha256': [upgrade.digest(old) if old else None]})
    (package / 'upgrade.json').write_text(json.dumps({'schema_version': 'research-upgrade-v1', 'files': entries}), encoding='utf-8')
    return project, package


def test_upgrade_then_rollback_preserves_original_and_removes_only_new_file(fixture, tmp_path):
    project, package = fixture
    checked = upgrade.inspect_upgrade(project, package)
    upgrade.change_files(checked, tmp_path / 'backup')
    assert (project / 'src/ashare_daily/one.py').read_bytes() == b'# new\n'
    assert (project / 'config/m3_daily.json').exists()
    upgrade.restore_files(checked)
    assert (project / 'src/ashare_daily/one.py').read_bytes() == b'# old\n'
    assert not (project / 'config/m3_daily.json').exists()


def test_unknown_server_edits_not_overwritten(fixture):
    project, package = fixture
    (project / 'src/ashare_daily/one.py').write_text('# user change\n', encoding='utf-8')
    with pytest.raises(ValueError, match='未识别修改'):
        upgrade.inspect_upgrade(project, package)


@pytest.mark.parametrize('name', ['.env', 'data/research/market.sqlite3', '../outside.py', '/absolute.py', 'config/unknown.json'])
def test_credentials_data_and_unlisted_paths_forbidden(fixture, name):
    project, package = fixture
    value = json.loads((package / 'upgrade.json').read_text())
    value['files'][0]['path'] = name
    (package / 'upgrade.json').write_text(json.dumps(value))
    with pytest.raises(ValueError, match='升级路径'):
        upgrade.inspect_upgrade(project, package)


def test_payload_tamper_rejected_before_changes(fixture):
    project, package = fixture
    (package / 'files/src/ashare_daily/one.py').write_bytes(b'# tampered\n')
    with pytest.raises(ValueError, match='校验失败'):
        upgrade.inspect_upgrade(project, package)
    assert (project / 'src/ashare_daily/one.py').read_bytes() == b'# old\n'


def test_partial_write_failure_rolls_back_prior_file(fixture, tmp_path, monkeypatch):
    project, package = fixture
    checked = upgrade.inspect_upgrade(project, package)
    original = upgrade.replace_file
    def fail(target, body, mode):
        if target.name == 'm3_daily.json':
            raise OSError('OFFLINE simulated disk error')
        original(target, body, mode)
    monkeypatch.setattr(upgrade, 'replace_file', fail)
    with pytest.raises(OSError):
        upgrade.change_files(checked, tmp_path / 'backup')
    assert (project / 'src/ashare_daily/one.py').read_bytes() == b'# old\n'


@pytest.mark.parametrize('exit_code', [0, 1])
def test_installer_fixtures_do_not_inherit_live_restore_map(tmp_path, monkeypatch, exit_code):
    from test_archive_paths import archive_fixture, test_original_host_without_map_keeps_existing_path_behavior
    from test_input_path_context import test_explicit_file_cannot_ignore_malformed_owner_metadata
    from ashare_daily.research.runner import _root

    project = archive_fixture(tmp_path / 'live-project', {}, source=str(ROOT))
    metadata = {name: (project / name).read_bytes() for name in ('restore-path-map.json', 'backup-manifest.json')}
    backup = project / 'outputs/verification/linux/upgrade'
    backup.mkdir(parents=True)
    observed = []

    def check_in_fixture_context(command, *, cwd, stdout, stderr, check):
        base = Path(command[command.index('--basetemp') + 1])
        base.mkdir()
        observed.append(base)
        assert cwd == project
        assert not base.is_relative_to(project)
        _root(base, offline=True)
        # Reuse the exact assertions that failed on the restored server.
        with monkeypatch.context() as context:
            context.chdir(project)
            test_original_host_without_map_keeps_existing_path_behavior(base)
            test_explicit_file_cannot_ignore_malformed_owner_metadata(base, context, False)
        stdout.write('OFFLINE regression result\n')
        return subprocess.CompletedProcess(command, exit_code)

    monkeypatch.setattr(upgrade.subprocess, 'run', check_in_fixture_context)
    result = upgrade.run_regression(Path(sys.executable), project, backup)
    assert result.returncode == exit_code
    assert (backup / 'pytest.txt').read_text() == 'OFFLINE regression result\n'
    assert observed and not observed[0].parent.exists()
    assert metadata == {name: (project / name).read_bytes() for name in metadata}
