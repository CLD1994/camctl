#!/usr/bin/env python3.11
"""构建只包含 camctl wheel、离线依赖和元数据的更新包。"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile


def _support(project):
    path = project / 'src/camctl/update_engine/bundle_support.py'
    spec = importlib.util.spec_from_file_location('camctl_update_bundle_support', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_payload(project, bundle, build_venv, run):
    support = _support(project)
    dist = bundle / 'camctl'
    dist.mkdir()
    run('导出锁定依赖', ['uv', 'export', '--project', project, '--frozen', '--no-dev',
                       '--no-emit-project', '--no-header', '-o', dist / 'requirements.txt'])
    run('构建 wheel', ['uv', 'build', '--project', project, '--python', sys.executable,
                     '--wheel', '--out-dir', dist])
    (dist / '.gitignore').unlink(missing_ok=True)
    wheels = list(dist.glob('*.whl'))
    if len(wheels) != 1:
        raise ValueError('wheel 必须恰好有一个')
    run('创建构建验证环境', ['uv', 'venv', '--python', sys.executable, build_venv])
    run('安装下载工具', ['uv', 'pip', 'install', '--python', build_venv / 'bin/python', 'pip'])
    for arch, platforms in support.WHEEL_TARGETS.items():
        destination = dist / 'wheelhouse' / arch
        destination.mkdir(parents=True)
        platform_args = [part for value in platforms for part in ('--platform', value)]
        run('下载离线依赖 ' + arch, [build_venv / 'bin/python', '-m', 'pip', 'download',
            '--dest', destination, '--only-binary=:all:', '--implementation', 'cp',
            '--python-version', '311', '--abi', 'cp311', *platform_args,
            '--require-hashes', '-r', dist / 'requirements.txt'])
        if not list(destination.glob('*.whl')):
            raise ValueError('目标平台离线依赖为空: ' + arch)
    return wheels[0]


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    if args.force != (args.config is not None):
        parser.error('--force 与 --config 必须同时指定')
    if args.config is not None and not args.config.is_absolute():
        parser.error('--config 必须是绝对路径')
    config_text = args.config.read_text(encoding='utf-8') if args.config is not None else None
    if config_text is not None:
        tomllib.loads(config_text)
    repo, output = args.repo.resolve(), args.output.resolve()
    if sys.platform != 'linux' or sys.version_info[:2] != (3, 11):
        parser.error('构建需要 Linux 和 Python 3.11')
    if output.exists() or (output.is_relative_to(repo) and not output.is_relative_to(repo / 'tmp')):
        parser.error('输出目录必须不存在；仓库内只允许 tmp 下')
    if subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], cwd=repo, text=True).strip() != str(repo):
        parser.error('--repo 必须是仓库根目录')
    output.mkdir(parents=True)
    receipt = {'status': 'building', 'steps': []}
    environment = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
    environment['SOURCE_DATE_EPOCH'] = subprocess.check_output(
        ['git', 'show', '-s', '--format=%ct', 'HEAD'], cwd=repo, text=True).strip()
    def save():
        (output / 'build-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    def run(label, argv, cwd=None, stdout=None):
        with (output / 'build.log').open('ab') as log:
            command = list(map(str, argv))
            log.write(('$ ' + repr(command) + '\n').encode()); log.flush()
            result = subprocess.run(command, cwd=cwd, stdout=stdout or log, stderr=log, env=environment)
        receipt['steps'].append({'label': label, 'returncode': result.returncode})
        save()
        if result.returncode:
            raise RuntimeError(label + '失败；详见 build.log')
    try:
        names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=repo)
        source = output / 'source'
        manifest = {}
        for name in sorted(set(os.fsdecode(n) for n in names.split(b'\0') if n)):
            if not name.startswith(('apps/camctl/', 'protocol/', 'docs/camctl/')):
                continue
            src = repo / name
            if src.is_symlink():
                raise ValueError('源码快照不接受符号链接: ' + name)
            if not src.exists():
                continue
            dst = source / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            dst.chmod(src.stat().st_mode & 0o777)
            manifest[name] = _sha(dst)
        project = source / 'apps/camctl'
        bundle = output / 'camctl-update'
        bundle.mkdir()
        build_payload(project, bundle, output / 'build-venv', run)
        support = _support(project)
        release = {'format_version': 1,
            'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip(),
            'git_status': subprocess.check_output(['git', 'status', '--porcelain=v1'], cwd=repo, text=True).splitlines(),
            'source_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
            'components': {'camctl': tomllib.loads((project / 'pyproject.toml').read_text())['project']['version']},
            'wheel_targets': support.wheel_target_manifest(bundle),
            'lock_sha256': _sha(project / 'uv.lock')}
        release['update'] = {'force': args.force}
        if args.force:
            release['update']['config_toml'] = config_text
        support.update_metadata(release)
        for name, value in (('RELEASE.json', release), ('source-manifest.json', manifest)):
            (bundle / name).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
        files = sorted(p for p in bundle.rglob('*') if p.is_file())
        (bundle / 'SHA256SUMS').write_text(''.join(_sha(p) + '  ' + p.relative_to(bundle).as_posix() + '\n' for p in files))
        support.verify_checksums(bundle)
        archive_path = output / 'camctl-update.zip'
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(p for p in bundle.rglob('*') if p.is_file()):
                info = zipfile.ZipInfo('camctl-update/' + path.relative_to(bundle).as_posix(), (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        (output / 'camctl-update.zip.sha256').write_text(_sha(archive_path) + '  camctl-update.zip\n')
        receipt.update(status='complete', archive=str(archive_path), sha256=_sha(archive_path))
        save()
        print(archive_path)
        return 0
    except Exception as error:
        receipt.update(status='failed', error=str(error))
        save()
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
