"""Offline failure/ownership tests. Do not claim actual nginx or LAN verification."""
import importlib.util
import ipaddress
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('nginx_intranet', Path(__file__).resolve().parents[1] / 'scripts/nginx_intranet.py')
lan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lan)

# Independent fictional values. CLI tests never consult the developer's private file.
ENDPOINT = {'server_ip': '192.168.56.10', 'listen_port': 18502}


@pytest.fixture(autouse=True)
def isolated_deployment_default(tmp_path, monkeypatch):
    load = lan.load_settings
    monkeypatch.setattr(lan, 'load_settings', lambda path=None: load(path, default_root=tmp_path))


def cli(argv):
    return lan.main([*argv, '--server-ip', ENDPOINT['server_ip'],
                     '--listen-port', str(ENDPOINT['listen_port'])])



@pytest.mark.parametrize('address', ['0.0.0.0', '8.8.8.8', '127.0.0.1', '::1', '10.0.0.0/8', '192.168.56.10', '192.168.80.60;\ninclude /etc/*;'])
def test_reject_expansion_or_injection(address):
    with pytest.raises(ValueError):
        lan.render(address, **ENDPOINT)


def test_preview_restricts_actual_tcp_peer_and_preserves_origin_and_websocket():
    text = lan.render('192.168.80.60', **ENDPOINT)
    assert 'listen 192.168.56.10:18502;' in text
    assert 'geo $realip_remote_addr ' in text  # Not a spoofable request header.
    assert 'default 0;' in text and '192.168.80.60/32 1;' in text
    assert 'if ($host != 192.168.56.10) { return 444; }' in text
    assert '0.0.0.0:' not in text and '10.0.0.0/8' not in text
    assert 'proxy_pass http://127.0.0.1:8501;' in text
    assert 'proxy_set_header Host $http_host;' in text
    assert 'proxy_set_header Upgrade $http_upgrade;' in text
    assert 'proxy_http_version 1.1;' in text
    assert '.env' not in text and 'root ' not in text and 'alias ' not in text


def callbacks():
    calls = []
    return calls, dict(validate=lambda present: calls.append(('validate', present)),
                       reload=lambda: calls.append(('reload',)), health=lambda: calls.append(('health',)))


def test_install_repeat_remove_and_backup(tmp_path):
    target = tmp_path / 'test.conf'
    content = lan.render('192.168.80.60', **ENDPOINT)
    calls, cb = callbacks()
    assert lan.change('install', target, content, **cb, **ENDPOINT)['changed']
    assert calls == [('validate', False), ('validate', True), ('reload',), ('health',)]
    calls.clear()
    assert not lan.change('install', target, content, **cb, **ENDPOINT)['changed']
    assert ('reload',) not in calls
    result = lan.change('remove', target, None, **cb, **ENDPOINT)
    assert not target.exists()
    assert Path(result['backup']).read_text() == content
    assert not list(tmp_path.glob('*.conf'))
    assert not lan.change('remove', target, None, **cb, **ENDPOINT)['changed']


@pytest.mark.parametrize('action', ['install', 'update', 'remove'])
def test_preserve_unrelated_or_manually_modified_file(tmp_path, action):
    target = tmp_path / 'test.conf'
    content = lan.render('192.168.80.60', **ENDPOINT) + '# manual change\n'
    target.write_text(content)
    _, cb = callbacks()
    with pytest.raises(ValueError, match='未覆盖或删除'):
        lan.change(action, target, lan.render('192.168.80.60', **ENDPOINT), **cb, **ENDPOINT)
    assert target.read_text() == content


def test_changed_client_ip_requires_explicit_remove(tmp_path):
    target = tmp_path / 'test.conf'
    content = lan.render('192.168.80.60', **ENDPOINT)
    target.write_text(content)
    _, cb = callbacks()
    with pytest.raises(ValueError, match='IP 不同'):
        lan.change('install', target, lan.render('192.168.80.61', **ENDPOINT), **cb, **ENDPOINT)
    assert target.read_text() == content


def test_bad_new_config_removed_before_rollback_reload(tmp_path):
    target = tmp_path / 'test.conf'
    calls, cb = callbacks()
    def validate(present):
        assert present == target.exists()
        if present:
            raise RuntimeError('OFFLINE invalid configuration')
    cb['validate'] = validate
    with pytest.raises(RuntimeError, match='磁盘配置已恢复'):
        lan.change('install', target, lan.render('192.168.80.60', **ENDPOINT), **cb, **ENDPOINT)
    assert not target.exists()
    assert calls == [('reload',)]  # Only reverted configuration reloaded.


def test_health_failure_rolls_back_new_file(tmp_path):
    target = tmp_path / 'test.conf'
    calls, cb = callbacks()
    def unhealthy():
        raise RuntimeError('OFFLINE health failure')
    cb['health'] = unhealthy
    with pytest.raises(RuntimeError, match='磁盘配置已恢复'):
        lan.change('install', target, lan.render('192.168.80.60', **ENDPOINT), **cb, **ENDPOINT)
    assert not target.exists()
    assert calls.count(('reload',)) == 2


def test_remove_failure_restores_original(tmp_path):
    target = tmp_path / 'test.conf'
    content = lan.render('192.168.80.60', **ENDPOINT)
    target.write_text(content)
    calls, cb = callbacks()
    def validate(present):
        if not present:
            raise RuntimeError('OFFLINE dependent config')
    cb['validate'] = validate
    with pytest.raises(RuntimeError, match='磁盘配置已恢复'):
        lan.change('remove', target, None, **cb, **ENDPOINT)
    assert target.read_text() == content
    assert len(list(tmp_path.glob('*.disabled'))) == 1


def test_atomic_writer_never_overwrites_existing(tmp_path):
    target = tmp_path / 'test.conf'
    target.write_text('pres unrelated')
    with pytest.raises(FileExistsError):
        lan.write_new(target, 'new')
    assert target.read_text() == 'pres unrelated'
    assert not list(tmp_path.glob('*.pending'))


def test_rollback_error_is_not_reported_as_success(tmp_path):
    target = tmp_path / 'test.conf'
    _, cb = callbacks()
    def failed_reload():
        raise OSError('OFFLINE failed reload')
    cb['reload'] = failed_reload
    with pytest.raises(RuntimeError, match='回退未能确认'):
        lan.change('install', target, lan.render('192.168.80.60', **ENDPOINT), **cb, **ENDPOINT)


def test_main_preview_makes_no_process_or_network_calls(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('Preview must not touch server')
    monkeypatch.setattr(lan, 'server_lock_and_master', forbidden)
    monkeypatch.setattr(lan, 'check_health', forbidden)
    assert cli(['preview', '--client-ip', '192.168.80.60']) == 0
    assert '"changed": false' in capsys.readouterr().out


@pytest.mark.parametrize('value', ['0.0.0.0/0', '8.8.8.0/24', '127.0.0.0/8', '192.168.80.60/20',
                                  '192.168.80.60', '192.168.80.60/32', '::/0', '172.0.0.0/8',
                                  '192.168.80.0/20;\ninclude /etc/*;'])
def test_network_scope_is_explicit_and_private(value):
    with pytest.raises(ValueError):
        lan.render(client_network=value, **ENDPOINT)


def test_office_mask_scope_and_adjacent_network_boundary():
    # Independent input: a fictitious /20 mask, not renderer-derived input.
    office = ipaddress.ip_network('192.168.80.60/255.255.240.0', strict=False)
    assert str(office) == '192.168.80.0/20'
    assert ipaddress.ip_address('192.168.80.65') in office
    assert ipaddress.ip_address('192.168.95.254') in office
    assert ipaddress.ip_address('192.168.96.1') not in office
    text = lan.render(client_network=str(office), **ENDPOINT)
    assert '192.168.80.0/20 1;' in text
    assert 'default 0;' in text
    assert '10.0.0.0/8 1;' not in text
    assert 'geo $realip_remote_addr ' in text
    assert 'listen 192.168.56.10:18502;' in text


def test_cannot_mix_host_and_network():
    with pytest.raises(ValueError):
        lan.render('192.168.80.60', client_network='192.168.80.0/20', **ENDPOINT)


def test_update_keeps_original_backup_and_is_idempotent(tmp_path):
    target = tmp_path / 'test.conf'
    old = lan.render('192.168.80.60', **ENDPOINT)
    new = lan.render(client_network='192.168.80.0/20', **ENDPOINT)
    target.write_text(old)
    calls, cb = callbacks()
    result = lan.change('update', target, new, **cb, **ENDPOINT)
    assert result['status'] == 'updated_server_probe_ok'
    assert Path(result['backup']).read_text() == old
    assert lan.owned_content(target, **ENDPOINT) == new
    assert calls == [('validate', True), ('validate', True), ('reload',), ('health',)]
    calls.clear()
    assert not lan.change('update', target, new, **cb, **ENDPOINT)['changed']
    assert ('reload',) not in calls
    assert len(list(tmp_path.glob('*.disabled'))) == 1
    # An explicit update can also restore the previous single-computer policy.
    lan.change('update', target, old, **cb, **ENDPOINT)
    assert target.read_text() == old


@pytest.mark.parametrize('failure', ['validate', 'health'])
def test_update_failure_restores_old_ip_policy(tmp_path, failure):
    target = tmp_path / 'test.conf'
    old = lan.render('192.168.80.60', **ENDPOINT)
    new = lan.render(client_network='192.168.80.0/20', **ENDPOINT)
    target.write_text(old)
    calls, cb = callbacks()
    def reject_new(*args):
        if target.read_text() == new:
            raise RuntimeError('OFFLINE reject widened configuration')
    cb[failure] = reject_new
    with pytest.raises(RuntimeError, match='磁盘配置已恢复'):
        lan.change('update', target, new, **cb, **ENDPOINT)
    assert target.read_text() == old
    assert len(list(tmp_path.glob('*.disabled'))) == 1


def test_update_requires_existing_owned_configuration(tmp_path):
    target = tmp_path / 'test.conf'
    _, cb = callbacks()
    with pytest.raises(ValueError, match='尚未安装'):
        lan.change('update', target, lan.render(client_network='192.168.80.0/20', **ENDPOINT), **cb, **ENDPOINT)
    assert not target.exists()


def test_replace_never_overwrites_intervening_changes(tmp_path):
    target = tmp_path / 'test.conf'
    expected = lan.render('192.168.80.60', **ENDPOINT)
    other = lan.render('192.168.80.61', **ENDPOINT)
    target.write_text(other)
    with pytest.raises(ValueError, match='检查后已改变'):
        lan.replace_owned(target, expected, lan.render(client_network='192.168.80.0/20', **ENDPOINT), **ENDPOINT)
    assert target.read_text() == other
    assert not list(tmp_path.glob('*.pending'))


def test_failed_atomic_replacement_keeps_previous_policy(tmp_path, monkeypatch):
    target = tmp_path / 'test.conf'
    old = lan.render('192.168.80.60', **ENDPOINT)
    target.write_text(old)
    def failed(*args):
        raise OSError('OFFLINE rename failure')
    monkeypatch.setattr(lan.os, 'replace', failed)
    _, cb = callbacks()
    with pytest.raises(OSError):
        lan.change('update', target, lan.render(client_network='192.168.80.0/20', **ENDPOINT), **cb, **ENDPOINT)
    assert target.read_text() == old
    assert not list(tmp_path.glob('*.pending'))


def test_network_preview_has_no_external_side_effects(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('Network preview must not operate on server')
    monkeypatch.setattr(lan, 'server_lock_and_master', forbidden)
    assert cli(['preview', '--client-network', '192.168.80.0/20']) == 0
    assert '192.168.80.0/20 1;' in capsys.readouterr().out


def test_v1_reference_configuration_keeps_exact_bytes():
    # Original renderer with the independent fictional endpoint, captured before refactoring.
    raw = lan.render('192.168.80.60', **ENDPOINT).encode('utf-8')
    assert len(raw) == 1580
    assert hashlib.sha256(raw).hexdigest() == 'f6ab4b74e8a8786519de5ae27a5e40c095901897c47bedce12a8ba1bb7d79f52'


def test_v2_reference_configuration_keeps_exact_bytes():
    # Original renderer with the independent fictional endpoint, captured before refactoring.
    assert hashlib.sha256(lan.render(client_network='192.168.80.0/20', **ENDPOINT).encode()).hexdigest() == (
        'af83e662742634455bcce6e3773bdbc3aae71c51d3915a00dd92bf6f968d6c76')


@pytest.mark.parametrize('policy', [{'client_ip': '192.168.80.60'}, {'client_network': '192.168.80.0/20'}])
def test_add_one_client_preserves_original_scope_and_every_other_directive(tmp_path, policy):
    original = lan.render(**policy, **ENDPOINT)
    expanded = lan.add_client(original, '192.168.96.17', **ENDPOINT)
    assert expanded.startswith(lan.EXTRA_CLIENT_MARKER + '\n')
    assert '# extra_client_ips=192.168.96.17\n' in expanded
    assert expanded.count('    192.168.96.17/32 1;\n') == 1
    restored = expanded.replace(lan.EXTRA_CLIENT_MARKER, original.splitlines()[0], 1).replace(
        '# extra_client_ips=192.168.96.17\n', '', 1).replace('    192.168.96.17/32 1;\n', '', 1)
    assert restored == original
    target = tmp_path / 'test.conf'
    target.write_text(expanded, encoding='utf-8')
    assert lan.owned_content(target, **ENDPOINT) == expanded
    assert lan.add_client(expanded, '192.168.96.17', **ENDPOINT) == expanded


@pytest.mark.parametrize('address', ['192.168.96.0/24', '192.168.96.17/32', '0.0.0.0', '8.8.8.8',
                                    '127.0.0.1', '::1', '192.168.56.10', '192.168.96.17;\nallow all;'])
def test_add_client_never_accepts_network_public_peer_or_injection(address):
    with pytest.raises(ValueError):
        lan.add_client(lan.render(client_network='192.168.80.0/20', **ENDPOINT), address, **ENDPOINT)


def test_add_covered_client_keeps_original_v2_and_does_not_reload_or_backup(tmp_path):
    target = tmp_path / 'test.conf'
    original = lan.render(client_network='192.168.80.0/20', **ENDPOINT)
    target.write_text(original, encoding='utf-8')
    calls, cb = callbacks()
    result = lan.change('add-client', target, '192.168.80.60', **cb, **ENDPOINT)
    assert result == {'status': 'already_installed', 'changed': False}
    assert target.read_text(encoding='utf-8') == original
    assert calls == [('validate', True), ('health',)]
    assert not list(tmp_path.glob('*.disabled'))


def test_add_client_transaction_backs_up_reuses_and_can_remove_v3(tmp_path):
    target = tmp_path / 'test.conf'
    original = lan.render(client_network='192.168.80.0/20', **ENDPOINT)
    target.write_text(original, encoding='utf-8')
    calls, cb = callbacks()
    result = lan.change('add-client', target, '192.168.96.17', **cb, **ENDPOINT)
    expanded = lan.add_client(original, '192.168.96.17', **ENDPOINT)
    assert result['status'] == 'updated_server_probe_ok'
    assert Path(result['backup']).read_text(encoding='utf-8') == original
    assert target.read_text(encoding='utf-8') == expanded
    assert calls == [('validate', True), ('validate', True), ('reload',), ('health',)]
    calls.clear()
    assert not lan.change('add-client', target, '192.168.96.17', **cb, **ENDPOINT)['changed']
    assert ('reload',) not in calls and len(list(tmp_path.glob('*.disabled'))) == 1
    assert lan.change('remove', target, None, **cb, **ENDPOINT)['changed']
    assert not target.exists()


@pytest.mark.parametrize('failure', ['validate', 'health'])
def test_add_client_failure_restores_exact_existing_scope(tmp_path, failure):
    target = tmp_path / 'test.conf'
    original = lan.render(client_network='192.168.80.0/20', **ENDPOINT)
    target.write_text(original, encoding='utf-8')
    _, cb = callbacks()
    def reject_new(*args):
        if target.read_text(encoding='utf-8') != original:
            raise RuntimeError('OFFLINE reject extra peer')
    cb[failure] = reject_new
    with pytest.raises(RuntimeError, match='磁盘配置已恢复'):
        lan.change('add-client', target, '192.168.96.17', **cb, **ENDPOINT)
    assert target.read_text(encoding='utf-8') == original


def test_add_client_refuses_missing_or_manually_changed_existing_policy(tmp_path):
    target = tmp_path / 'test.conf'
    _, cb = callbacks()
    with pytest.raises(ValueError, match='尚未安装'):
        lan.change('add-client', target, '192.168.96.17', **cb, **ENDPOINT)
    expanded = lan.add_client(lan.render('192.168.80.60', **ENDPOINT), '192.168.96.17', **ENDPOINT)
    target.write_text(expanded.replace('default 0;', 'default 1;'), encoding='utf-8')
    with pytest.raises(ValueError, match='未覆盖或删除'):
        lan.change('add-client', target, '192.168.96.18', **cb, **ENDPOINT)
    assert 'default 1;' in target.read_text(encoding='utf-8')


def test_add_client_cli_accepts_only_an_explicit_single_ip(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid addition must not touch server')
    monkeypatch.setattr(lan, 'server_lock_and_master', forbidden)
    assert cli(['add-client', '--client-network', '192.168.96.0/24']) == 1
    assert cli(['add-client']) == 1
    assert '单个已核对客户端' in capsys.readouterr().out


def test_v3_reference_configuration_keeps_exact_bytes():
    # Original renderer with the independent fictional endpoint, captured before refactoring.
    raw = lan.add_client(lan.render(client_network='192.168.80.0/20', **ENDPOINT), '192.168.96.17', **ENDPOINT).encode()
    assert len(raw) == 1643
    assert hashlib.sha256(raw).hexdigest() == '0ee07342ec710c37699582c9ac3a3b6442ac1a5e3ad916a7317fea56bce48f56'


def test_all_clients_removes_ip_restrictions_and_preserves_proxy_directives():
    content = lan.render(all_clients=True, **ENDPOINT)
    assert content.startswith('# ashare-daily-research intranet v4\n')
    assert 'geo ' not in content
    assert '$ashare_daily_intranet_allowed' not in content
    assert 'return 403;' not in content
    assert '/32 1;' not in content
    # Only the IP policy changes; every other nginx directive stays identical.
    original = lan.render('192.168.80.60', **ENDPOINT)
    original_tail = original[original.index('map $http_upgrade'):].replace(
        '    if ($ashare_daily_intranet_allowed = 0) { return 403; }\n', '', 1)
    assert content[content.index('map $http_upgrade'):] == original_tail
    assert content.count('listen ') == 1
    assert 'listen 192.168.56.10:18502;' in content
    assert 'if ($host != 192.168.56.10) { return 444; }' in content
    assert 'proxy_pass http://127.0.0.1:8501;' in content


def test_v4_reference_configuration_keeps_exact_bytes():
    # Original renderer with the independent fictional endpoint, captured before refactoring.
    raw = lan.render(all_clients=True, **ENDPOINT).encode()
    assert len(raw) == 1307
    assert hashlib.sha256(raw).hexdigest() == 'de4e8f6bb7930c167802949814845eedb9d4ed53f0e0625b6c2b8c2f21a3203e'


@pytest.mark.parametrize('policy', [{'client_ip': '192.168.80.60'}, {'client_network': '192.168.80.0/20'}])
def test_all_clients_cannot_mix_with_an_ip_policy(policy):
    with pytest.raises(ValueError):
        lan.render(all_clients=True, **policy, **ENDPOINT)


@pytest.mark.parametrize('alteration', ['append', 'host', 'bind', 'upstream'])
def test_all_clients_ownership_rejects_manual_modifications(tmp_path, alteration):
    target = tmp_path / 'test.conf'
    original = lan.render(all_clients=True, **ENDPOINT)
    target.write_text(original, encoding='utf-8')
    assert lan.owned_content(target, **ENDPOINT) == original
    replacements = {
        'append': (original, original + '# manual change\n'),
        'host': ('if ($host != 192.168.56.10) { return 444; }', ''),
        'bind': ('listen 192.168.56.10:18502;', 'listen 0.0.0.0:18502;'),
        'upstream': ('http://127.0.0.1:8501;', 'http://127.0.0.1:8503;'),
    }
    target.write_text(original.replace(*replacements[alteration]), encoding='utf-8')
    with pytest.raises(ValueError, match='未覆盖或删除'):
        lan.owned_content(target, **ENDPOINT)


@pytest.mark.parametrize('version', [1, 2, 3])
def test_all_clients_update_retains_backup_and_can_restore_or_remove(tmp_path, version):
    old = lan.render('192.168.80.60', **ENDPOINT) if version == 1 else lan.render(client_network='192.168.80.0/20', **ENDPOINT)
    if version == 3:
        old = lan.add_client(old, '192.168.96.17', **ENDPOINT)
    target = tmp_path / 'test.conf'
    target.write_text(old, encoding='utf-8')
    opened = lan.render(all_clients=True, **ENDPOINT)
    calls, cb = callbacks()
    result = lan.change('update', target, opened, **cb, **ENDPOINT)
    assert result['status'] == 'updated_server_probe_ok'
    assert Path(result['backup']).read_text(encoding='utf-8') == old
    assert lan.owned_content(target, **ENDPOINT) == opened
    assert calls == [('validate', True), ('validate', True), ('reload',), ('health',)]
    calls.clear()
    assert not lan.change('update', target, opened, **cb, **ENDPOINT)['changed']
    assert calls == [('validate', True), ('health',)]
    assert len(list(tmp_path.glob('*.disabled'))) == 1
    lan.change('update', target, old, **cb, **ENDPOINT)
    assert target.read_text(encoding='utf-8') == old
    lan.change('update', target, opened, **cb, **ENDPOINT)
    removed = lan.change('remove', target, None, **cb, **ENDPOINT)
    assert not target.exists()
    assert Path(removed['backup']).read_text(encoding='utf-8') == opened


@pytest.mark.parametrize('failure', ['validate', 'reload', 'health'])
def test_all_clients_update_failure_restores_original_policy(tmp_path, failure):
    target = tmp_path / 'test.conf'
    old = lan.add_client(lan.render(client_network='192.168.80.0/20', **ENDPOINT), '192.168.96.17', **ENDPOINT)
    target.write_text(old, encoding='utf-8')
    opened = lan.render(all_clients=True, **ENDPOINT)
    _, cb = callbacks()
    def reject_new(*args):
        if target.read_text(encoding='utf-8') == opened:
            raise RuntimeError('OFFLINE reject open configuration')
    cb[failure] = reject_new
    with pytest.raises(RuntimeError, match='磁盘配置已恢复'):
        lan.change('update', target, opened, **cb, **ENDPOINT)
    assert target.read_text(encoding='utf-8') == old
    assert len(list(tmp_path.glob('*.disabled'))) == 1


def test_add_client_rejects_all_clients_without_changing_or_backing_up(tmp_path):
    target = tmp_path / 'test.conf'
    opened = lan.render(all_clients=True, **ENDPOINT)
    target.write_text(opened, encoding='utf-8')
    calls, cb = callbacks()
    with pytest.raises(ValueError, match='所有客户端'):
        lan.change('add-client', target, '192.168.96.17', **cb, **ENDPOINT)
    assert target.read_text(encoding='utf-8') == opened
    assert calls == []
    assert not list(tmp_path.glob('*.disabled'))


def test_all_clients_preview_has_no_server_side_effects(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('All-clients preview must not touch server')
    monkeypatch.setattr(lan, 'server_lock_and_master', forbidden)
    monkeypatch.setattr(lan, 'check_health', forbidden)
    assert cli(['preview', '--all-clients']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['configuration'] == lan.render(all_clients=True, **ENDPOINT)
    assert '不限制来源 IP' in result['transport']
    assert result['external_calls'] == 0 and result['changed'] is False


@pytest.mark.parametrize('action', ['install', 'update'])
def test_all_clients_cli_runs_existing_transaction(tmp_path, monkeypatch, capsys, action):
    target = tmp_path / 'test.conf'
    opened = lan.render(all_clients=True, **ENDPOINT)
    target.write_text(opened if action == 'install' else lan.render('192.168.80.60', **ENDPOINT), encoding='utf-8')
    monkeypatch.setattr(lan, 'VHOST', target)
    monkeypatch.setattr(lan, 'server_lock_and_master', lambda: nullcontext(lambda: None))
    monkeypatch.setattr(lan, 'check_health', lambda *args, **kwargs: None)
    monkeypatch.setattr(lan, 'validate_nginx', lambda present: None)
    assert cli([action, '--all-clients']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == ('already_installed' if action == 'install' else 'updated_server_probe_ok')
    assert lan.owned_content(target, **ENDPOINT) == opened


@pytest.mark.parametrize('action', ['preview', 'install', 'update'])
@pytest.mark.parametrize('scope', [['--client-ip', '192.168.80.60'], ['--client-network', '192.168.80.0/20']])
def test_all_clients_cli_rejects_mixed_scopes(action, scope):
    with pytest.raises(SystemExit) as exc:
        cli([action, '--all-clients', *scope])
    assert exc.value.code == 2


def test_add_client_cli_rejects_all_clients_before_server_access(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid addition must not touch server')
    monkeypatch.setattr(lan, 'server_lock_and_master', forbidden)
    assert cli(['add-client', '--all-clients']) == 1
    assert '单个已核对客户端' in capsys.readouterr().out


def test_deployment_config_supplies_endpoint_without_server_access(tmp_path, monkeypatch, capsys):
    config = tmp_path / 'deployment.json'
    config.write_text(json.dumps({'server_ip': '192.168.56.10', 'listen_port': 18502}), encoding='utf-8')
    def forbidden(*args, **kwargs):
        pytest.fail('Preview must not touch server')
    monkeypatch.setattr(lan, 'server_lock_and_master', forbidden)
    monkeypatch.setattr(lan, 'check_health', forbidden)
    assert lan.main(['preview', '--all-clients', '--deployment-config', str(config)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['url'] == 'http://192.168.56.10:18502'
    assert 'listen 192.168.56.10:18502;' in result['configuration']


def test_deployment_cli_overrides_config_without_leaking_to_next_call(tmp_path, capsys):
    config = tmp_path / 'deployment.json'
    config.write_text(json.dumps({'server_ip': '192.168.56.10', 'listen_port': 18502}), encoding='utf-8')
    args = ['preview', '--all-clients', '--deployment-config', str(config)]
    assert lan.main([*args, '--server-ip', '192.168.57.10', '--listen-port', '18503']) == 0
    overridden = json.loads(capsys.readouterr().out)
    assert overridden['url'] == 'http://192.168.57.10:18503'
    assert 'listen 192.168.57.10:18503;' in overridden['configuration']
    assert lan.main(args) == 0
    assert json.loads(capsys.readouterr().out)['url'] == 'http://192.168.56.10:18502'


@pytest.mark.parametrize('settings', [{}, {'server_ip': '192.168.56.10'}, {'listen_port': 18502}])
@pytest.mark.parametrize('action', ['preview', 'install', 'update', 'remove', 'add-client'])
def test_deployment_missing_endpoint_has_no_host_effects(tmp_path, monkeypatch, capsys, settings, action):
    config = tmp_path / 'deployment.json'
    config.write_text(json.dumps(settings), encoding='utf-8')
    def forbidden(*args, **kwargs):
        pytest.fail('Missing deployment parameters must not touch server')
    monkeypatch.setattr(lan, 'server_lock_and_master', forbidden)
    monkeypatch.setattr(lan, 'check_health', forbidden)
    args = [action, '--client-ip', '192.168.80.60', '--deployment-config', str(config)]
    assert lan.main(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'failed'
    assert 'server_ip' in result['reason'] or 'listen_port' in result['reason']


@pytest.mark.parametrize('address', ['0.0.0.0', '8.8.8.8', '127.0.0.1', '::1',
                                     '192.168.56.0/24', 'host.invalid', '192.168.56.10;include /etc/*;'])
def test_deployment_invalid_server_address_has_no_host_effects(tmp_path, monkeypatch, capsys, address):
    config = tmp_path / 'deployment.json'
    config.write_text('{}', encoding='utf-8')
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid deployment parameters must not touch server')
    monkeypatch.setattr(lan, 'server_lock_and_master', forbidden)
    monkeypatch.setattr(lan, 'check_health', forbidden)
    assert lan.main(['install', '--all-clients', '--deployment-config', str(config),
                     '--server-ip', address, '--listen-port', '18502']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'


@pytest.mark.parametrize('port', [0, -1, 65536])
def test_deployment_invalid_port_has_no_host_effects(tmp_path, monkeypatch, capsys, port):
    config = tmp_path / 'deployment.json'
    config.write_text('{}', encoding='utf-8')
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid deployment parameters must not touch server')
    monkeypatch.setattr(lan, 'server_lock_and_master', forbidden)
    assert lan.main(['install', '--all-clients', '--deployment-config', str(config),
                     '--server-ip', '192.168.56.10', '--listen-port', str(port)]) == 1
    assert 'listen_port' in json.loads(capsys.readouterr().out)['reason']


@pytest.mark.parametrize('explicit_missing_file', [False, True])
def test_deployment_absent_config_fails_without_guessing_endpoint(tmp_path, monkeypatch, capsys,
                                                               explicit_missing_file):
    def forbidden(*args, **kwargs):
        pytest.fail('Absent deployment configuration must not touch server')
    monkeypatch.setattr(lan, 'server_lock_and_master', forbidden)
    args = ['install', '--all-clients']
    if explicit_missing_file:
        args.extend(['--deployment-config', str(tmp_path / 'missing.json')])
    assert lan.main(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'failed'
    assert '不存在' in result['reason'] if explicit_missing_file else 'server_ip' in result['reason']


@pytest.mark.parametrize('endpoint', [
    {'server_ip': '192.168.57.10', 'listen_port': 18502},
    {'server_ip': '192.168.56.10', 'listen_port': 18503},
])
def test_ownership_rejects_valid_config_for_a_different_endpoint(tmp_path, endpoint):
    target = tmp_path / 'test.conf'
    original = lan.render(all_clients=True, **ENDPOINT)
    target.write_text(original, encoding='utf-8')
    calls, cb = callbacks()
    with pytest.raises(ValueError, match='未覆盖或删除'):
        lan.change('remove', target, None, **endpoint, **cb)
    assert target.read_text(encoding='utf-8') == original
    assert calls == []
    assert not list(tmp_path.glob('*.disabled'))


@pytest.mark.parametrize('action', ['install', 'update', 'remove'])
def test_rollback_preserves_intervening_owned_configuration(tmp_path, action):
    target = tmp_path / 'test.conf'
    original = lan.render('192.168.80.60', **ENDPOINT)
    desired = lan.render(client_network='192.168.80.0/20', **ENDPOINT)
    intervening = lan.render('192.168.80.61', **ENDPOINT)
    if action != 'install':
        target.write_text(original, encoding='utf-8')
    calls, cb = callbacks()
    def concurrent_change():
        target.write_text(intervening, encoding='utf-8')
        raise RuntimeError('OFFLINE concurrent replacement before failed reload')
    cb['reload'] = concurrent_change
    with pytest.raises(RuntimeError, match='回退未能确认'):
        lan.change(action, target, None if action == 'remove' else desired, **ENDPOINT, **cb)
    assert target.read_text(encoding='utf-8') == intervening
    assert calls == [('validate', action != 'install'), ('validate', action != 'remove')]
    assert not list(tmp_path.glob('*.pending'))
