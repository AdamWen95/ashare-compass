"""Build a code/config-only upgrade, never include .env, SQLite or raw responses."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile

_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)
from deployment_settings import add_deployment_argument, load_settings, resolve_setting

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    'src/ashare_daily/cli.py', 'src/ashare_daily/eligibility.py', 'src/ashare_daily/qualification_sources.py',
    'src/ashare_daily/operations/daily.py', 'src/ashare_daily/reports/reader.py', 'src/ashare_daily/reports/m3_render.py',
    'src/ashare_daily/research/associations.py', 'src/ashare_daily/research/discovery.py',
    'src/ashare_daily/research/contracts.py', 'src/ashare_daily/research/evidence.py',
    'src/ashare_daily/research/preparation.py', 'src/ashare_daily/research/runner.py',
    'src/ashare_daily/research/sources.py', 'src/ashare_daily/viewer.py',
    'config/m3_daily.json', 'config/eligibility_sources.json', 'config/m3_sources.json', 'config/m4.json',
    'tests/test_research_enhancement.py', 'tests/test_research_upgrade.py',
    'tests/test_m4_daily.py', 'tests/test_m4_relocated_daily.py', 'tests/test_m4_viewer.py',
    'scripts/apply_research_upgrade.py', 'scripts/apply_deployment_fix.py', 'scripts/deployment_settings.py',
]


def digest(body):
    return hashlib.sha256(body).hexdigest()


def build(root=ROOT, *, deployment_config=None, project=None, account=None, ssh_host=None):
    settings = load_settings(deployment_config, default_root=root)
    project = resolve_setting(settings, 'project_root', project, required=True)
    account = resolve_setting(settings, 'account', account)
    ssh_host = resolve_setting(settings, 'ssh_host', ssh_host, required=True)
    profile = (Path(deployment_config) if deployment_config is not None else root / '.local/deployment.json').resolve()
    report = root / 'outputs/research/m4/reports/2026-09-10/20260911T113313925244-121992df'
    required = [*(root / name for name in FILES), root / 'docs/31_RESEARCH_COMPLETION.md',
                report / 'daily_brief.html', report / 'daily_brief.md']
    if any(path.resolve() == profile for path in required):
        raise ValueError('部署配置与升级必需文件冲突；请使用独立部署配置文件')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = root / 'deploy' / ('ashare-research-upgrade-' + stamp)
    directory.mkdir(exist_ok=False)
    old_bodies = {name: [] for name in FILES}
    for name in FILES:
        before = root / 'outputs/verification/enhancement/before' / name.replace('/', '__')
        if before.exists():
            old_bodies[name].append(before.read_bytes())
    for filename in ('ashare-debian-20260910.tar.gz', 'ashare-debian-fix-20260910.tar.gz'):
        with tarfile.open(root / 'deploy' / filename) as archive:
            for item in archive.getmembers():
                for name in FILES:
                    if item.isfile() and item.name in ('project/' + name, 'ashare-debian-fix-20260910/files/' + name):
                        old_bodies[name].append(archive.extractfile(item).read())
    entries = []
    for name in FILES:
        body = (root / name).read_bytes()
        target = directory / 'files' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        hashes = set()
        for old in old_bodies[name]:
            hashes.add(digest(old))
            lf = old.replace(b'\r\n', b'\n')
            hashes.add(digest(lf))
            hashes.add(digest(lf.replace(b'\n', b'\r\n')))
        entries.append({'path': name, 'sha256': digest(body), 'accepted_previous_sha256': sorted(hashes) if hashes else [None]})
    for name in ('apply_research_upgrade.py', 'apply_deployment_fix.py', 'deployment_settings.py'):
        shutil.copyfile(root / 'scripts' / name, directory / name)
    shutil.copyfile(root / 'docs/31_RESEARCH_COMPLETION.md', directory / 'README.md')
    manifest = {'schema_version': 'research-upgrade-v1', 'created_at': datetime.now(timezone.utc).isoformat(), 'files': entries}
    (directory / 'upgrade.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (directory / 'preview').mkdir()
    for name in ('daily_brief.html', 'daily_brief.md'):
        shutil.copyfile(report / name, directory / 'preview' / name)
    archive_path = directory.with_name(directory.name + '.tar.gz')
    install = ('# Run as ' + (account or 'the existing project user') + ' in the existing SSH session, not with sudo.\n'
               'cd ~\nsha256sum -c ' + archive_path.name + '.sha256 &&\n'
               'tar -xzf ' + archive_path.name + ' &&\n'
               + project + '/.venv/bin/python ./' + directory.name +
               '/apply_research_upgrade.py --project ' + project + ' --apply --refresh-latest\n')
    (directory / 'INSTALL.txt').write_text(install, encoding='utf-8')
    upload = ('# Run in local Windows PowerShell using the configured SSH host.\n'
              'scp "' + str(archive_path) + '" "' + str(archive_path) + '.sha256" ' + ssh_host + ':~/\n'
              'if ($LASTEXITCODE -ne 0) { throw "Upload failed; do not start installation." }\n')
    (directory / 'UPLOAD.ps1').write_text(upload, encoding='utf-8')
    with tarfile.open(archive_path, 'w:gz') as archive:
        for path in sorted(directory.rglob('*')):
            if path.is_file():
                archive.add(path, arcname=directory.name + '/' + path.relative_to(directory).as_posix())
    checksum = digest(archive_path.read_bytes())
    archive_path.with_name(archive_path.name + '.sha256').write_text(checksum + '  ' + archive_path.name + '\n', encoding='ascii')
    result = {'directory': str(directory), 'archive': str(archive_path), 'sha256': checksum, 'files': len(entries)}
    (root / 'outputs/verification/enhancement/package.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', help='Linux目标项目绝对路径')
    parser.add_argument('--account', help='现有项目普通用户')
    parser.add_argument('--ssh-host', help='已配置的SSH主机别名或主机名')
    add_deployment_argument(parser)
    args = parser.parse_args(argv)
    build(deployment_config=args.deployment_config, project=args.project, account=args.account, ssh_host=args.ssh_host)


if __name__ == '__main__':
    main()
