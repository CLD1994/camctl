#!/usr/bin/env python3.11
"""从当前源码快照构建、验证并交付包含三个组件的 ZIP。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile


SKILL_PATH = Path('.agents/skills/camctl-real-device-deploy')
BUNDLE_NAME = 'camctl-deploy'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')


def write_checksums(bundle):
    files = sorted(p for p in bundle.rglob('*') if p.is_file() and p != bundle / 'SHA256SUMS')
    (bundle / 'SHA256SUMS').write_text(''.join(
        sha(p) + '  ' + p.relative_to(bundle).as_posix() + '\n' for p in files), encoding='utf-8')


def write_configuration_docs(source, bundle):
    """从权威配置文档导出随包参考，移除未随包交付的仓库链接。"""
    docs = bundle / 'docs'
    docs.mkdir()

    def standalone(text):
        def link(match):
            label, target = match.groups()
            return match[0] if target.startswith(('https://', 'http://', '#')) else label
        return re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link, text)

    reference = (source / 'docs/camctl/configuration-reference.md').read_text(encoding='utf-8')
    (docs / 'camctl-configuration.md').write_text(standalone(reference), encoding='utf-8')
    host = (source / 'apps/host-demo/README.md').read_text(encoding='utf-8')
    heading = '## 配置范围与日志\n'
    if heading not in host:
        raise ValueError('host 配置参考章节缺失，请同步打包入口')
    section = host.split(heading, 1)[1].split('\n## ', 1)[0]
    (docs / 'host-configuration.md').write_text('# camctl_host 配置与日志\n' + standalone(section), encoding='utf-8')
    shutil.copyfile(source / 'apps/host-demo/include/camctl_host.h', docs / 'camctl_host.h')


def write_zip(bundle, destination):
    # 条目顺序、时间和权限不依赖运行日期及构建目录的 umask。
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(p for p in bundle.rglob('*') if p.is_file()):
            info = zipfile.ZipInfo(BUNDLE_NAME + '/' + path.relative_to(bundle).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if path.suffix == '.sh' else 0o644
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def snapshot(repo, target):
    names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=repo)
    prefixes = ('apps/camctl/', 'apps/client/', 'apps/host-demo/', 'protocol/', 'docs/camctl/', str(SKILL_PATH) + '/')
    manifest = {}
    for name in sorted(set(os.fsdecode(n) for n in names.split(b'\0') if n)):
        if not name.startswith(prefixes):
            continue
        source = repo / name
        if source.is_symlink():
            raise ValueError('源码快照不接受符号链接：' + name)
        if not source.exists():
            continue  # 当前工作树删除保持删除。
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(source.stat().st_mode & 0o777)
        manifest[name] = sha(destination)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, required=True, help='尚不存在的输出目录；仓库内仅允许 tmp 下')
    parser.add_argument('--config', type=Path, help='仅用于客户端声明的实际主机配置；省略时生成占位声明')
    args = parser.parse_args()
    repo, output = args.repo.resolve(), args.output.resolve()
    if sys.version_info[:2] != (3, 11) or sys.platform != 'linux':
        parser.error('构建需要 Linux 和 Python 3.11')
    if output.exists():
        parser.error('输出目录已存在，请使用新目录')
    if output.is_relative_to(repo) and not output.is_relative_to(repo / 'tmp'):
        parser.error('仓库内输出目录必须位于 tmp 下')
    for tool in ('uv', 'git', 'cmake', 'cpack', 'node', 'npm', 'cc'):
        if not shutil.which(tool):
            parser.error('缺少构建工具：' + tool)
    try:
        config_bytes = args.config.read_bytes() if args.config else None
    except OSError as error:
        parser.error('无法读取声明配置：' + str(error))
    if subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], cwd=repo, text=True).strip() != str(repo):
        parser.error('--repo 必须指向 Git 工作树根目录')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    status = subprocess.check_output(['git', 'status', '--porcelain=v1'], cwd=repo, text=True).splitlines()
    output.mkdir(parents=True)
    environment = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
    environment['SOURCE_DATE_EPOCH'] = subprocess.check_output(
        ['git', 'show', '-s', '--format=%ct', 'HEAD'], cwd=repo, text=True).strip()
    receipt = {'status': 'building', 'steps': []}

    def run(label, argv, cwd=None, stdout=None):
        print(label, flush=True)
        command = list(map(str, argv))
        with (output / 'build.log').open('ab') as log:
            log.write(('\n' + label + ': ' + repr(command) + '\n').encode()); log.flush()
            result = subprocess.run(command, cwd=cwd, stdout=stdout or log, stderr=log, env=environment)
        receipt['steps'].append({'label': label, 'returncode': result.returncode})
        write_json(output / 'build-receipt.json', receipt)
        if result.returncode:
            raise RuntimeError(label + ' 失败；详见 build.log')

    try:
        source = output / 'source'
        manifest = snapshot(repo, source)
        skill = source / SKILL_PATH
        bundle = output / BUNDLE_NAME
        shutil.copytree(skill / 'assets/bundle', bundle)
        write_configuration_docs(source, bundle)
        for name in ('install-host.py', 'deploy_support.py', 'camera_discovery.py'):
            shutil.copyfile(skill / 'scripts' / name, bundle / name)
        dist = bundle / 'camctl'
        dist.mkdir()
        project = source / 'apps/camctl'
        run('导出锁定依赖', ['uv', 'export', '--project', project, '--frozen', '--no-dev', '--no-emit-project', '--no-header', '-o', dist / 'requirements.txt'])
        run('构建 wheel', ['uv', 'build', '--project', project, '--python', sys.executable, '--wheel', '--out-dir', dist])
        # uv 自动写出的 .gitignore 不属于部署内容。
        (dist / '.gitignore').unlink(missing_ok=True)
        wheels = list(dist.glob('*.whl'))
        if len(wheels) != 1:
            raise ValueError('wheel 必须恰好有一个')
        venv = output / 'build-venv'
        run('创建构建验证环境', ['uv', 'venv', '--python', sys.executable, venv])
        run('安装锁定依赖', ['uv', 'pip', 'install', '--python', venv / 'bin/python', '--require-hashes', '-r', dist / 'requirements.txt'])
        run('安装本次 wheel', ['uv', 'pip', 'install', '--python', venv / 'bin/python', '--no-deps', wheels[0]])
        run('检查依赖', ['uv', 'pip', 'check', '--python', venv / 'bin/python'])
        config = output / 'capabilities-config.toml'
        if config_bytes is None:
            # 驱动标识与部署策略仍只从发现脚本维护，不复制一份设备清单。
            code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                    'from camera_discovery import add_placeholder_devices; '
                    'from deploy_support import write_config; from pathlib import Path; '
                    'r={"devices":{},"placeholder_devices":{},"warnings":[]}; '
                    'add_placeholder_devices(r); write_config(Path(sys.argv[2]), {"devices":r["devices"]})')
            run('生成占位配置', [venv / 'bin/python', '-c', code, skill / 'scripts', config])
        else:
            config.write_bytes(config_bytes)
        data = bundle / 'client/data'
        data.mkdir()
        with (data / 'device-capabilities.json').open('wb') as out:
            run('用本次 wheel 导出设备声明', [venv / 'bin/camctl', 'describe', '--config', config], stdout=out)
        host_build = output / 'host-build'
        host_build.mkdir()
        run('配置 host 源码打包', ['cmake', source / 'apps/host-demo', '-DCMAKE_BUILD_TYPE=Release', '-DHOST_BUILD_TESTS=OFF'], cwd=host_build)
        run('生成 CPack 源码包', ['cpack', '--config', 'CPackSourceConfig.cmake'], cwd=host_build)
        archives = list(host_build.glob('*-Source.tar.gz'))
        if len(archives) != 1:
            raise ValueError('host 源码包必须恰好有一个')
        (bundle / 'host').mkdir()
        shutil.copyfile(archives[0], bundle / 'host' / archives[0].name)
        client_source = source / 'apps/client'
        run('安装客户端构建依赖', ['npm', 'ci', '--include=dev', '--engine-strict', '--no-audit', '--no-fund'], cwd=client_source)
        run('构建客户端网页', ['npm', 'run', 'build'], cwd=client_source)
        client = bundle / 'client/apps/client'
        client.mkdir(parents=True)
        for name in ('package.json', 'package-lock.json'):
            shutil.copyfile(client_source / name, client / name)
        for name in ('server', 'shared', 'domain'):
            shutil.copytree(client_source / 'src' / name, client / 'src' / name)
        shutil.copytree(client_source / 'dist', client / 'dist')
        shutil.copytree(source / 'protocol/schemas', bundle / 'client/protocol/schemas')
        (bundle / 'client/protocol/errors').mkdir()
        shutil.copyfile(source / 'protocol/errors/workflow-codes.json', bundle / 'client/protocol/errors/workflow-codes.json')
        client_meta = json.loads((client / 'package.json').read_text())
        cmake_config = (host_build / 'CPackConfig.cmake').read_text()
        host_version = re.search(r'set\(CPACK_PACKAGE_VERSION "([^"]+)"\)', cmake_config)[1]
        release = {
            'format_version': 1, 'git_head': head, 'git_status': status,
            'source_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
            'components': {'camctl': tomllib.loads((project / 'pyproject.toml').read_text())['project']['version'],
                           'host': host_version, 'client': client_meta['version']},
            'locks': {name: sha(source / name) for name in ('apps/camctl/uv.lock', 'apps/client/package-lock.json')},
            'capabilities': {'mode': 'configured' if config_bytes is not None else 'placeholder', 'config_sha256': sha(config),
                             'sha256': sha(data / 'device-capabilities.json')},
            'tools': {name: subprocess.check_output([name, '--version'], text=True).splitlines()[0]
                      for name in ('uv', 'cmake', 'node', 'npm')},
            'python': sys.version.split()[0],
            'validation': {'status': 'pending'},
        }
        write_json(bundle / 'source-manifest.json', manifest)
        write_json(bundle / 'RELEASE.json', release)
        write_checksums(bundle)
        candidate = output / 'candidate.zip'
        write_zip(bundle, candidate)
        report = output / 'validation.json'
        run('仓库外验证交付物', [sys.executable, skill / 'scripts/verify_bundle.py', candidate, '--report', report])
        release['validation'] = json.loads(report.read_text())
        write_json(bundle / 'RELEASE.json', release)
        write_checksums(bundle)
        final = output / (BUNDLE_NAME + '.zip')
        write_zip(bundle, final)
        with zipfile.ZipFile(final) as archive:
            if archive.testzip():
                raise ValueError('最终 ZIP 数据校验失败')
        (output / (final.name + '.sha256')).write_text(sha(final) + '  ' + final.name + '\n')
        candidate.unlink()
        receipt.update(status='complete', archive=str(final), sha256=sha(final))
        write_json(output / 'build-receipt.json', receipt)
        print('交付完成：' + str(final))
        return 0
    except Exception as error:
        receipt.update(status='failed', error=str(error))
        write_json(output / 'build-receipt.json', receipt)
        print(str(error) + '；保留输出目录与日志，重试须使用新目录。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
