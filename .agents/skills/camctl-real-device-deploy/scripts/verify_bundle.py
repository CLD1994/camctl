#!/usr/bin/env python3.11
"""在仓库之外验证 ZIP 的主机安装、声明同步及客户端实际启动。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import zipfile


def verify_checksums(bundle):
    expected = {}
    for line in (bundle / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ', 1)
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or name in expected:
            raise ValueError('校验清单路径无效：' + name)
        expected[name] = digest
    actual = {p.relative_to(bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in bundle.rglob('*') if p.is_file() and p != bundle / 'SHA256SUMS'}
    if expected != actual:
        raise ValueError('交付文件集合或摘要与 SHA256SUMS 不一致')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    workspace = Path(tempfile.mkdtemp(prefix='camctl-bundle-check-', dir='/tmp'))
    report = {'status': 'running', 'platform': platform.system(), 'architecture': platform.machine(),
              'checks': [], 'not_run': ['完整组件测试', '最低 CMake 兼容性', 'Windows', 'ARM64', '真实设备操作']}
    environment = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'CAMCTL_DATA_DIR', 'ADB_SERVER_SOCKET')}

    def run(argv, cwd, env=environment):
        with (workspace / 'verification.log').open('ab') as log:
            log.write(('\n' + repr(list(map(str, argv))) + '\n').encode()); log.flush()
            subprocess.run(list(map(str, argv)), cwd=cwd, env=env, stdout=log, stderr=log, check=True, timeout=600)

    try:
        with zipfile.ZipFile(args.archive) as archive:
            if archive.testzip():
                raise ValueError('ZIP 数据校验失败')
            for member in archive.infolist():
                path = Path(member.filename)
                if path.is_absolute() or '..' in path.parts or path.parts[0] != 'camctl-deploy':
                    raise ValueError('ZIP 目录结构无效')
            archive.extractall(workspace)
        bundle = workspace / 'camctl-deploy'
        verify_checksums(bundle)
        report['checks'].append('ZIP 与完整文件校验清单')
        readme = (bundle / 'README.md').read_text(encoding='utf-8')
        for target_link in re.findall(r'\[[^\]]+\]\(([^)]+)\)', readme):
            if not target_link.startswith(('http://', 'https://', '#')):
                if not (bundle / target_link.split('#', 1)[0]).is_file():
                    raise ValueError('README 引用了未交付文件：' + target_link)
        for name in ('camctl-configuration.md', 'host-configuration.md', 'camctl_host.h'):
            if not (bundle / 'docs' / name).is_file():
                raise ValueError('配置参考缺失：' + name)
        report['checks'].append('README 链接与随包配置参考')
        tools = workspace / 'tools'
        tools.mkdir()
        # 与设备身份命令的真实输出契约一致；不调用现场相机。
        for name, body in {
            'adb': "#!/bin/sh\n[ \"$*\" = 'devices -l' ] || exit 99\nprintf 'List of devices attached\\n'\n",
            'hb_cli': "#!/bin/sh\n[ \"$*\" = '--pid 0x000a list' ] || exit 99\nprintf '(no Hasselblad USB devices found)\\n'\n",
        }.items():
            path = tools / name
            path.write_text(body)
            path.chmod(0o755)
        isolated = {**environment, 'PATH': str(tools) + os.pathsep + environment['PATH']}
        target = workspace / 'host instance'
        run([sys.executable, bundle / 'install-host.py', '--target', target], workspace, isolated)
        receipt = json.loads((target / 'install-receipt.json').read_text())
        if receipt['status'] != 'installed_stopped':
            raise ValueError('主机安装未完成')
        config = tomllib.loads((target / 'config.toml').read_text())
        with sqlite3.connect(config['paths']['state_db']) as db:
            if db.execute('pragma integrity_check').fetchone()[0] != 'ok':
                raise ValueError('状态库完整性检查失败')
        for path in (target / 'start-host-demo.sh', target / 'host/bin/host-demo'):
            if not path.is_file():
                raise ValueError('安装入口缺失：' + str(path))
        run(['bash', '-n', target / 'start-host-demo.sh'], workspace)
        examples = re.findall(r'```c\n(.*?)```', readme, re.S)
        if not examples:
            raise ValueError('README 缺少 C 接入示例')
        for index, example in enumerate(examples):
            source = workspace / ('readme-example-' + str(index) + '.c')
            source.write_text(example, encoding='utf-8')
            run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-c', source,
                 '-I', target / 'host/include', '-o', source.with_suffix('.o')], workspace)
        report['checks'].append('README C 接入示例按本次安装的头文件编译')
        toml_examples = re.findall(r'```toml\n(.*?)```', readme, re.S)
        if not toml_examples:
            raise ValueError('README 缺少 camctl 配置示例')
        example_config = workspace / 'readme-config.toml'
        example_config.write_text('\n'.join(toml_examples).replace('/home/user/.camctl/device-test', str(target)), encoding='utf-8')
        run([target / 'venv/bin/camctl', 'describe', '--config', example_config], workspace)
        report['checks'].append('README TOML 示例由本次安装的 camctl 校验')
        cap = target / 'device-capabilities.json'
        if (target / 'client-data/device-capabilities.json').read_bytes() != cap.read_bytes():
            raise ValueError('默认客户端声明同步结果不一致')
        report['checks'].append('身份查询替身下的主机安装、配置、状态库与声明同步')
        # 显式同步到含既有业务文件的目录；安装不得修改数据库与媒体。
        shared_data = workspace / 'existing client data'
        shared_data.mkdir()
        retained = {'client.db': b'existing-client-database', 'uploads/media.jpg': b'existing-media'}
        for name, contents in retained.items():
            path = shared_data / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        other = workspace / 'second host instance'
        custom_paths = {key: str(workspace / 'custom host paths' / name) for key, name in {
            'state-db': 'state.db', 'log-file': 'logs/camctl.log', 'staging': 'staging',
            'ready': 'ready', 'processing': 'processing'}.items()}
        custom_args = [value for key, path in custom_paths.items() for value in ('--' + key, path)]
        run([sys.executable, bundle / 'install-host.py', '--target', other, '--client-data', shared_data,
             *custom_args], workspace, isolated)
        configured = tomllib.loads((other / 'config.toml').read_text())
        if any(configured['paths'][key.replace('-', '_')] != value for key, value in custom_paths.items()):
            raise ValueError('安装参数没有写入最终 camctl 配置')
        launcher = (other / 'start-host-demo.sh').read_text()
        command = shlex.split(next(line[5:] for line in launcher.splitlines() if line.startswith('exec ')))
        for flag in ('ready', 'processing'):
            if command[command.index('--' + flag) + 1] != custom_paths[flag]:
                raise ValueError('host-demo 与 camctl 交接目录不一致')
        if command[command.index('--log') + 1] != str(other / 'host.log'):
            raise ValueError('安装器混用了 Python 日志和 C 模块日志路径')
        if (shared_data / 'device-capabilities.json').read_bytes() != (other / 'device-capabilities.json').read_bytes():
            raise ValueError('显式客户端声明同步结果不一致')
        if any((shared_data / name).read_bytes() != contents for name, contents in retained.items()):
            raise ValueError('声明同步修改了客户端业务文件')
        report['checks'].append('自定义安装路径、两端交接目录与日志区分、声明同步保留既有数据')
        client = bundle / 'client'
        run(['npm', 'ci', '--omit=dev', '--engine-strict', '--no-audit', '--no-fund'], client / 'apps/client')
        custom_data = workspace / 'custom client data'
        custom_data.mkdir()
        shutil.copyfile(client / 'data/device-capabilities.json', custom_data / 'device-capabilities.json')
        for mode, expected_data, data_environment in (
            ('default', client / 'data', {}),
            ('custom', custom_data, {'CAMCTL_DATA_DIR': str(custom_data)}),
        ):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            client_log = workspace / ('client-' + mode + '.log')
            with client_log.open('wb') as log:
                process = subprocess.Popen(['sh', client / 'start-client.sh'], env={**environment, **data_environment, 'PORT': str(port), 'HOST': '127.0.0.1'},
                                           cwd=workspace, stdout=log, stderr=log, start_new_session=True)
                try:
                    deadline = time.monotonic() + 45
                    while True:
                        if process.poll() is not None:
                            raise ValueError('客户端提前退出；详见 ' + str(client_log))
                        try:
                            with urllib.request.urlopen(f'http://localhost:{port}/api/state', timeout=1) as response:
                                state = json.load(response)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError('客户端启动超过 45 秒')
                            time.sleep(0.2)
                    if state['startup']['state'] != 'uninitialized' or state['startup']['directory'] != str(expected_data):
                        raise ValueError('客户端没有使用指定数据目录：' + str(expected_data))
                    if state['capabilities']['error'] or state['capabilities']['active'] is None:
                        raise ValueError('客户端无法加载包内设备声明')
                    expected = json.loads((client / 'data/device-capabilities.json').read_text())
                    if {d['device_id'] for d in expected['devices']} != {d['device_id'] for d in state['capabilities']['active']['devices']}:
                        raise ValueError('客户端启用的设备与包内声明不一致')
                    with urllib.request.urlopen(f'http://localhost:{port}/', timeout=3) as response:
                        if b'<html' not in response.read().lower():
                            raise ValueError('客户端未提供构建后的网页')
                    report['checks'].append('客户端生产依赖、启动脚本、网页与声明加载；目录模式：' + mode)
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass  # 服务已退出时仍保留上面的原始启动错误。
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        report['status'] = 'passed'
        print('交付物验证通过；日志：' + str(workspace))
        return 0
    except Exception as error:
        report.update(status='failed', error=str(error), evidence=str(workspace))
        print('交付物验证失败：' + str(error) + '；日志：' + str(workspace), file=sys.stderr)
        return 1
    finally:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
