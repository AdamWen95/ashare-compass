"""One optional intranet vhost for the verified BaoTa installation; stdlib only.

Does not read project data/credentials or modify nginx.conf, systemd or firewalls.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from deployment_settings import add_deployment_argument, load_settings, resolve_setting

NGINX = Path('/www/server/nginx/sbin/nginx')
MAIN = Path('/www/server/nginx/conf/nginx.conf')
PID = Path('/www/server/nginx/logs/nginx.pid')
VHOST = Path('/www/server/panel/vhost/nginx/ashare-daily-research-intranet.conf')
MARKER = '# ashare-daily-research intranet v1'
NETWORK_MARKER = '# ashare-daily-research intranet v2'
EXTRA_CLIENT_MARKER = '# ashare-daily-research intranet v3'
ALL_CLIENTS_MARKER = '# ashare-daily-research intranet v4'
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def client_address(value: str, *, server_ip: str) -> str:
    server_ip = resolve_setting({}, 'server_ip', server_ip, required=True)
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ValueError('请提供一个办公电脑的 IPv4 地址，不能填网段或主机名') from exc
    if not any(address in n for n in PRIVATE_NETWORKS) or str(address) == server_ip:
        raise ValueError('仅接受与服务器不同的单个 RFC1918 内网 IPv4 地址')
    return str(address)


def network_address(value: str) -> str:
    try:
        network = ipaddress.IPv4Network(value, strict=True)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError) as exc:
        raise ValueError('请提供明确的IPv4网段，例如192.168.80.0/20；不能把主机地址自动扩大为网段') from exc
    if '/' not in value or network.prefixlen == 32 or not any(network.subnet_of(n) for n in PRIVATE_NETWORKS):
        raise ValueError('仅接受完整位于RFC1918范围内的明确网段；单个IP请用 --client-ip')
    return str(network)


def render(client_ip: str | None = None, *, client_network: str | None = None,
           all_clients: bool = False, server_ip: str, listen_port: int) -> str:
    server_ip = resolve_setting({}, 'server_ip', server_ip, required=True)
    listen_port = resolve_setting({}, 'listen_port', listen_port, required=True)
    if sum((client_ip is not None, client_network is not None, bool(all_clients))) != 1:
        raise ValueError('必须且只能指定一个办公电脑IP、一个办公网段或 --all-clients')
    if all_clients:
        policy = f'''{ALL_CLIENTS_MARKER}
# Plain HTTP, no client IP restriction; no password authentication.
'''
        ip_guard = ''
    else:
        if client_network is not None:
            source = network_address(client_network)
            header = f'{NETWORK_MARKER}\n# client_network={source}'
        else:
            client_ip = client_address(client_ip, server_ip=server_ip)
            source = client_ip + '/32'
            header = f'{MARKER}\n# client_ip={client_ip}'
        policy = f'''{header}
# Plain HTTP, original TCP peer restriction; no password authentication.
# Server self-address is permitted for local health checks.
geo $realip_remote_addr $ashare_daily_intranet_allowed {{
    default 0;
    {source} 1;
    {server_ip}/32 1;
}}
'''
        ip_guard = '    if ($ashare_daily_intranet_allowed = 0) { return 403; }\n'
    return f'''{policy}map $http_upgrade $ashare_daily_intranet_connection {{
    default upgrade;
    '' close;
}}
log_format ashare_daily_intranet '$time_iso8601 $realip_remote_addr $status $request_method';
server {{
    listen {server_ip}:{listen_port};
    server_name {server_ip};
    server_tokens off;
    access_log /www/server/nginx/logs/ashare-daily-research-intranet.access.log ashare_daily_intranet;
    error_log /www/server/nginx/logs/ashare-daily-research-intranet.error.log error;
{ip_guard}    if ($host != {server_ip}) {{ return 444; }}
    location / {{
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $ashare_daily_intranet_connection;
        proxy_set_header X-Real-IP $realip_remote_addr;
        proxy_set_header X-Forwarded-For $realip_remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Authorization '';
        proxy_cache off;
        proxy_buffering off;
        proxy_connect_timeout 5s;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        add_header X-Content-Type-Options nosniff always;
    }}
}}
'''


def render_extra_clients(client_ip: str | None = None, *, client_network: str | None = None,
                         extra_client_ips=(), server_ip: str, listen_port: int) -> str:
    """Keep the original scope verbatim, adding only explicit private /32 peers."""
    original = render(client_ip, client_network=client_network, server_ip=server_ip, listen_port=listen_port)
    if not extra_client_ips:
        return original
    extras = [client_address(value, server_ip=server_ip) for value in extra_client_ips]
    primary = ipaddress.ip_network(client_network or client_ip + '/32')
    if len(set(extras)) != len(extras) or any(ipaddress.ip_address(value) in primary for value in extras):
        raise ValueError('额外客户端必须互不重复，且不能已在原访问范围内')
    extras.sort(key=lambda value: int(ipaddress.ip_address(value)))
    lines = original.splitlines(keepends=True)
    lines[0] = EXTRA_CLIENT_MARKER + '\n'
    lines.insert(2, '# extra_client_ips=' + ','.join(extras) + '\n')
    return ''.join(lines).replace(f'    {server_ip}/32 1;\n',
        ''.join(f'    {value}/32 1;\n' for value in extras) + f'    {server_ip}/32 1;\n', 1)


def _owned_policy(content: str, *, server_ip: str, listen_port: int) -> tuple[dict, list[str]]:
    endpoint = dict(server_ip=server_ip, listen_port=listen_port)
    if content == render(all_clients=True, **endpoint):
        return {'all_clients': True}, []
    match = re.match(re.escape(MARKER) + r'\n# client_ip=([^\n]+)\n', content)
    if match and content == render(match[1], **endpoint):
        return {'client_ip': match[1]}, []
    match = re.match(re.escape(NETWORK_MARKER) + r'\n# client_network=([^\n]+)\n', content)
    if match and content == render(client_network=match[1], **endpoint):
        return {'client_network': match[1]}, []
    match = re.match(re.escape(EXTRA_CLIENT_MARKER)
        + r'\n# (client_ip|client_network)=([^\n]+)\n# extra_client_ips=([^\n]+)\n', content)
    if match:
        policy, extras = {match[1]: match[2]}, match[3].split(',')
        if content == render_extra_clients(**policy, extra_client_ips=extras, **endpoint):
            return policy, extras
    raise ValueError('已有同名配置并非本脚本原样生成，未覆盖或删除')


def owned_content(path: Path, *, server_ip: str, listen_port: int) -> str | None:
    if path.is_symlink():
        raise ValueError('项目 vhost 是符号链接，停止操作')
    if not path.exists():
        return None
    content = path.read_text(encoding='utf-8')
    _owned_policy(content, server_ip=server_ip, listen_port=listen_port)
    return content


def add_client(content: str, client_ip: str, *, server_ip: str, listen_port: int) -> str:
    """Extend a strictly owned policy by one peer; covered peers are a no-op."""
    endpoint = dict(server_ip=server_ip, listen_port=listen_port)
    address = client_address(client_ip, server_ip=server_ip)
    policy, extras = _owned_policy(content, **endpoint)
    if policy.get('all_clients'):
        raise ValueError('当前入口已允许所有客户端，不能 add-client；需要改变访问范围请显式使用 update')
    primary = ipaddress.ip_network(policy.get('client_network') or policy['client_ip'] + '/32')
    if ipaddress.ip_address(address) in primary or address in extras:
        return content
    return render_extra_clients(**policy, extra_client_ips=[*extras, address], **endpoint)


def write_new(path: Path, content: str) -> None:
    """Publish a complete file without replacing an existing file or symlink."""
    _publish(path, content, expected=None)


def replace_owned(path: Path, expected: str, content: str, *, server_ip: str, listen_port: int) -> None:
    """Replace only the previously inspected owned configuration, atomically."""
    _publish(path, content, expected=expected, server_ip=server_ip, listen_port=listen_port)


def _publish(path: Path, content: str, *, expected: str | None,
             server_ip: str | None = None, listen_port: int | None = None) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.ashare-intranet-', suffix='.pending', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if expected is None:
            os.link(temporary, path)
        else:
            if owned_content(path, server_ip=server_ip, listen_port=listen_port) != expected:
                raise ValueError('配置在检查后已改变，拒绝替换')
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def change(action: str, path: Path, desired: str | None, *, validate, reload, health,
           server_ip: str, listen_port: int) -> dict:
    """Testable transaction: no edits to any pre-existing unrelated vhost."""
    endpoint = dict(server_ip=resolve_setting({}, 'server_ip', server_ip, required=True),
                    listen_port=resolve_setting({}, 'listen_port', listen_port, required=True))
    if action not in ('install', 'update', 'remove', 'add-client'):
        raise ValueError('未知操作')
    if action != 'remove' and desired is None:
        raise ValueError('缺少配置内容')
    old = owned_content(path, **endpoint)
    if action == 'add-client':
        if old is None:
            raise ValueError('尚未安装项目入口，不能 add-client')
        desired = add_client(old, desired, **endpoint)
        action = 'update'
    if action == 'install' and old is not None and old != desired:
        raise ValueError('已安装的办公电脑 IP 不同或范围变化；请显式使用 update')
    if action == 'update' and old is None:
        raise ValueError('尚未安装项目入口，不能 update；请先 install')
    validate(old is not None)
    if action == 'remove' and old is None:
        return {'status': 'already_absent', 'changed': False}
    if action != 'remove' and old == desired:
        health()
        return {'status': 'already_installed', 'changed': False}
    backup = None
    if old is not None:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = path.with_name(path.name + '.' + stamp + '.disabled')
        write_new(backup, old)
        if action == 'update':
            replace_owned(path, old, desired, **endpoint)
        else:
            if owned_content(path, **endpoint) != old:
                raise ValueError('配置在检查后已改变，拒绝删除')
            path.unlink()
    else:
        if desired is None:
            raise ValueError('缺少配置内容')
        write_new(path, desired)
    try:
        validate(action != 'remove')
        reload()
        if action != 'remove':
            health()
    except Exception as exc:
        try:
            if old is None:
                if owned_content(path, **endpoint) != desired:
                    raise ValueError('配置被其他操作改变，拒绝自动删除')
                path.unlink()
            elif action == 'remove':
                write_new(path, old)
            else:
                replace_owned(path, desired, old, **endpoint)
            validate(old is not None)
            reload()
        except Exception as rollback_error:
            raise RuntimeError('操作失败且回退未能确认；停止重试，请在服务器检查 Nginx。' +
                               f' 回退错误类型：{type(rollback_error).__name__}') from exc
        raise RuntimeError('操作未通过，磁盘配置已恢复并已发送回退重载；未宣称入口启用成功') from exc
    statuses = {'install': 'installed_server_probe_ok', 'update': 'updated_server_probe_ok',
                'remove': 'removed_reload_sent'}
    return {'status': statuses[action],
            'changed': True, 'backup': str(backup) if backup else None}


def validate_nginx(expect_loaded: bool) -> None:
    # Inspect the loaded-file marker privately; never print other sites' configuration.
    result = subprocess.run([str(NGINX), '-T', '-c', str(MAIN)], capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Nginx 检查失败；请在服务器运行 nginx -t 查看详细错误')
    marker = ('# configuration file ' + str(VHOST) + ':').encode()
    if (marker in result.stdout) != expect_loaded:
        raise RuntimeError('项目配置的实际加载情况与预期不同，未启用入口')


def check_health(url: str, *, attempts: int = 1) -> None:
    # Ignore shell proxy variables. Read-only health endpoint, never run-daily.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(attempts):
        try:
            with opener.open(url, timeout=3) as response:
                if response.status == 200 and response.read(128).strip() == b'ok':
                    return
        except (OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(1)
    raise RuntimeError('网页健康检查未通过；不会启动或重启 Streamlit')


@contextmanager
def server_lock_and_master():
    if not sys.platform.startswith('linux') or os.geteuid() != 0:
        raise ValueError('实际安装/撤销须在已核对的服务器 root 终端执行；本机只能 preview')
    import fcntl
    # Reject redirected installation paths before any mutation.
    for path in (NGINX, MAIN, PID, VHOST.parent):
        if not path.exists() or path.resolve() != path:
            raise ValueError('服务器路径缺失或含符号链接，停止操作：' + str(path))
    lock_path = MAIN.parent / '.ashare-intranet.lock'
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    master_fd = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        master_pid = int(PID.read_text().strip())
        if master_pid <= 1:
            raise ValueError('无效的 Nginx master PID')
        master_fd = os.pidfd_open(master_pid)
        proc = Path('/proc') / str(master_pid)
        command = (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
        if (not os.path.samefile(proc / 'exe', NGINX)
                or 'nginx: master process ' not in command
                or f'-c {MAIN}' not in command):
            raise ValueError('PID 文件对应的进程不是已核对的宝塔 Nginx master')
        yield lambda: signal.pidfd_send_signal(master_fd, signal.SIGHUP)
    finally:
        if master_fd is not None:
            os.close(master_fd)
        os.close(lock_fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='日报专用内网入口；不修改主配置、用户服务或防火墙')
    parser.add_argument('action', choices=('preview', 'install', 'update', 'remove', 'add-client'))
    add_deployment_argument(parser)
    parser.add_argument('--server-ip', help='已核对的服务器 RFC1918 IPv4 地址；覆盖本地部署配置')
    parser.add_argument('--listen-port', type=int, help='内网入口端口；覆盖本地部署配置')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--client-ip', help='单个办公电脑内网 IPv4，必须实际核对')
    scope.add_argument('--client-network', help='明确获准的办公IPv4网段，如192.168.80.0/20；不会自动推断')
    scope.add_argument('--all-clients', action='store_true', help='明确允许所有客户端，不限制来源IP；仍只绑定既有内网地址')
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.deployment_config)
        server_ip = resolve_setting(settings, 'server_ip', args.server_ip, required=True)
        listen_port = resolve_setting(settings, 'listen_port', args.listen_port, required=True)
        endpoint = dict(server_ip=server_ip, listen_port=listen_port)
        if args.action == 'add-client':
            if args.all_clients or args.client_network is not None or args.client_ip is None:
                raise ValueError('add-client 仅接受 --client-ip 单个已核对客户端，保留原访问范围')
            desired = client_address(args.client_ip, server_ip=server_ip)
        else:
            desired = render(args.client_ip, client_network=args.client_network,
                             all_clients=args.all_clients, **endpoint) if (
                args.client_ip or args.client_network or args.all_clients) else None
        if args.action != 'remove' and desired is None:
            raise ValueError('preview/install/update 必须显式提供 --client-ip、--client-network 或 --all-clients')
        if args.action == 'preview':
            print(json.dumps({'status': 'preview', 'url': f'http://{server_ip}:{listen_port}',
                              'vhost': VHOST.as_posix(), 'configuration': desired,
                              'transport': ('HTTP，未加密；不限制来源 IP，没有账号身份认证' if args.all_clients else
                                            'HTTP，未加密；按 TCP 来源 IP 限制，不是账号身份认证'),
                              'external_calls': 0, 'changed': False}, ensure_ascii=False, indent=2))
            return 0
        with server_lock_and_master() as reload:
            if args.action != 'remove':
                check_health('http://127.0.0.1:8501/_stcore/health')
                if not VHOST.exists():
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                        probe.bind((server_ip, listen_port))
            result = change(args.action, VHOST, desired, validate=validate_nginx, reload=reload,
                            health=lambda: check_health(f'http://{server_ip}:{listen_port}/_stcore/health', attempts=5),
                            **endpoint)
        result.update({'url': f'http://{server_ip}:{listen_port}', 'client_browser_verified': False,
                       'vhost': VHOST.as_posix(), 'model_calls': 0})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Generic OS errors can contain config text; never dump a subprocess's stdout/stderr.
        reason = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else '服务器检查失败，未确认启用；请回传错误类型以继续定位'
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__, 'reason': reason},
                         ensure_ascii=False, indent=2))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
