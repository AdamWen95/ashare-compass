"""Hash-checked application/config upgrade with backup, tests and rollback.

Run as the existing project user. No credential, database, nginx or timer edits.
The optional refresh is a new, budgeted research run using the last report date.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from uuid import uuid4

_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)
from deployment_settings import add_deployment_argument, load_settings, resolve_setting
from apply_deployment_fix import digest, no_links, read_file, replace_file

CONFIGS = {'config/m4.json', 'config/m3_daily.json', 'config/m3_sources.json', 'config/eligibility_sources.json'}
SCRIPTS = {'scripts/apply_research_upgrade.py', 'scripts/apply_deployment_fix.py', 'scripts/deployment_settings.py'}
UNIT = 'ashare-daily-research-web.service'


def inspect_upgrade(project: Path, package: Path) -> list[dict]:
    no_links(project)
    no_links(package)
    if not (project / 'src/ashare_daily').is_dir() or not (project / 'config/m4.json').is_file():
        raise ValueError('目标不是已安装的 A 股研究项目')
    manifest = json.loads(read_file(package / 'upgrade.json', 2000000))
    if manifest.get('schema_version') != 'research-upgrade-v1':
        raise ValueError('升级清单版本不符')
    entries = manifest.get('files')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError('升级文件数量超出有限范围')
    checked, seen = [], set()
    for entry in entries:
        name = entry.get('path', '')
        allowed = name in CONFIGS | SCRIPTS or re.fullmatch(r'(?:src/ashare_daily/(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*|tests/test_[A-Za-z0-9_]+)\.py', name)
        if not allowed or name.casefold() in seen:
            raise ValueError('非法或重复升级路径：' + name)
        seen.add(name.casefold())
        expected = entry.get('accepted_previous_sha256')
        new_hash = entry.get('sha256')
        if (not isinstance(expected, list) or not expected or len(expected) > 12
                or any(h is not None and (not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h)) for h in expected)
                or not isinstance(new_hash, str) or not re.fullmatch('[0-9a-f]{64}', new_hash)):
            raise ValueError('升级哈希结构无效')
        body = read_file(package / 'files' / name)
        if digest(body) != new_hash:
            raise ValueError('包内文件校验失败：' + name)
        if name.endswith('.py'):
            compile(body, name, 'exec')
        else:
            json.loads(body)
        target = project / name
        no_links(target)
        before = read_file(target) if target.exists() else None
        current = digest(before) if before is not None else None
        if current not in [*expected, new_hash]:
            raise ValueError('服务器文件有未识别修改，未覆盖：' + name)
        checked.append({'path': name, 'target': target, 'body': body, 'before': before,
                        'previous_sha256': current, 'sha256': new_hash,
                        'mode': target.stat().st_mode & 0o777 if target.exists() else 0o600})
    return checked


def change_files(checked, backup: Path):
    backup.mkdir(parents=True, exist_ok=False)
    for item in checked:
        if item['before'] is not None:
            target = backup / 'before' / item['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item['before'])
    manifest = [{k: item[k] for k in ('path', 'previous_sha256', 'sha256', 'mode')} for item in checked]
    (backup / 'rollback.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    changed = []
    try:
        for item in checked:
            current = digest(read_file(item['target'])) if item['target'].exists() else None
            if current != item['previous_sha256']:
                raise ValueError('应用前文件发生变化：' + item['path'])
            replace_file(item['target'], item['body'], item['mode'])
            changed.append(item)
    except BaseException:
        restore_files(changed)
        raise


def restore_files(checked):
    for item in reversed(checked):
        no_links(item['target'])
        if digest(read_file(item['target'])) != item['sha256']:
            raise ValueError('回滚前文件再次变化，保留并停止：' + item['path'])
        if item['before'] is None:
            item['target'].unlink()
        else:
            replace_file(item['target'], item['before'], item['mode'])


def run_regression(python: Path, project: Path, backup: Path):
    # Fixtures must not inherit the live project's restore-path-map.json.
    # Only the test log belongs in the backup. Linux uses a private /tmp
    # directory even if the user's TMPDIR points into the application.
    with tempfile.TemporaryDirectory(prefix='ashare-tests-',
            dir='/tmp' if sys.platform == 'linux' else None) as temporary:
        base = Path(temporary).resolve()
        if base.is_relative_to(project.resolve()):
            raise ValueError('测试临时目录必须位于项目目录之外')
        with (backup / 'pytest.txt').open('w', encoding='utf-8') as log:
            return subprocess.run([str(python), '-m', 'pytest', '-q', '--basetemp', str(base / 'cases')],
                cwd=project, stdout=log, stderr=subprocess.STDOUT, check=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, help='Linux目标项目绝对路径；也可由本地部署配置提供')
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--refresh-latest', action='store_true')
    add_deployment_argument(parser)
    args = parser.parse_args(argv)
    settings = load_settings(args.deployment_config)
    project = Path(resolve_setting(settings, 'project_root', args.project, required=True)).absolute()
    package = args.package.absolute()
    entries = inspect_upgrade(project, package)
    changes = [item for item in entries if item['previous_sha256'] != item['sha256']]
    print(json.dumps({'status': 'preview', 'project': str(project), 'files_to_change': [c['path'] for c in changes]}, ensure_ascii=False), flush=True)
    if not args.apply:
        return 0
    if sys.platform != 'linux' or os.geteuid() == 0:
        raise ValueError('部署应用步骤仅供 Linux 项目普通账号运行，不要 sudo')
    python = project / '.venv/bin/python'
    if not python.is_file():
        raise ValueError('找不到现有项目 Python 环境')
    sys.path.insert(0, str(project / 'src'))
    from ashare_daily.operations.lock import ProcessLock
    from ashare_daily.viewer import scan_reports
    reports, _ = scan_reports(project / 'outputs')
    last_date = reports[0].date if reports else None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    backup = project / 'outputs/verification/linux/research-upgrades' / stamp
    report = {'status': 'already_applied' if not changes else 'pending', 'backup_directory': str(backup), 'model_refresh': 'not_run'}
    with ProcessLock(project / 'data/operations/daily.lock', 'upgrade-' + stamp):
        if changes:
            web_active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', UNIT], check=False).returncode == 0
            if web_active:
                subprocess.run(['systemctl', '--user', 'stop', UNIT], check=True)
            applied = False
            try:
                change_files(changes, backup)
                applied = True
                print('代码和配置已更新；正在执行服务器回归测试。日志：' + str(backup / 'pytest.txt'), flush=True)
                check = run_regression(python, project, backup)
                if check.returncode:
                    raise RuntimeError('服务器回归未通过，详见 ' + str(backup / 'pytest.txt'))
                if web_active:
                    subprocess.run(['systemctl', '--user', 'start', UNIT], check=True)
                    for attempt in range(15):
                        try:
                            with urlopen('http://127.0.0.1:8501/_stcore/health', timeout=2) as response:
                                if response.status == 200:
                                    break
                        except OSError:
                            pass
                        time.sleep(1)
                    else:
                        raise RuntimeError('新版网页健康检查未通过')
                report['status'] = 'applied_and_verified'
            except BaseException:
                if applied:
                    restore_files(changes)
                    report['status'] = 'rolled_back'
                if web_active:
                    subprocess.run(['systemctl', '--user', 'restart', UNIT], check=False)
                if backup.exists():
                    (backup / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
                raise
    # Release the daily lock before the separately budgeted application run.
    if args.refresh_latest and last_date:
        backup.mkdir(parents=True, exist_ok=True)
        print('正在按最近已发布交易日 ' + last_date + ' 生成新版本；模型调用仍受每日预算限制。', flush=True)
        with (backup / 'refresh-latest.txt').open('w', encoding='utf-8') as log:
            run = subprocess.run([str(python), '-u', '-m', 'ashare_daily', 'run-daily', '--date', last_date],
                cwd=project, stdout=log, stderr=subprocess.STDOUT, check=False)
        report['model_refresh'] = 'finished' if run.returncode in (0, 1) else 'failed_or_not_generated'
        report['refresh_exit_code'] = run.returncode
    if backup.exists():
        (backup / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['model_refresh'] != 'failed_or_not_generated' else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
