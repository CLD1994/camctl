#!/usr/bin/env python3.11
"""停止 host 和全部 CLI 后，离线更新原部署中的 camctl。"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import uuid
import zipfile

# 每次启动或重新执行时，先阻止导入在包内写入缓存。
sys.dont_write_bytecode = True

from bundle_support import current_target, verify_checksums, wheel_target_manifest


def perform_update(ops):
    """在更新期间保持两把锁，并保存每一步的实际结果。"""
    receipt = {'status': 'checking', 'steps': [], 'environment_may_have_changed': False}
    locked = False
    try:
        receipt['original'] = ops.preflight()
        ops.save(receipt)
        ops.lock(receipt['original'])
        locked = True
        ops.check_original(receipt['original'])
        ops.save(receipt)
        receipt['environment_may_have_changed'] = True
        ops.save(receipt)
        ops.install(receipt)
        ops.check_new(receipt)
        receipt['status'] = 'updated_stopped'
    except Exception as error:
        receipt.update(status='failed', error=f'{type(error).__name__}: {error}')
    finally:
        if locked:
            try:
                ops.unlock()
            except Exception as error:
                receipt.update(status='failed', unlock_error=f'{type(error).__name__}: {error}')
        ops.save(receipt)
    return receipt


class UpdateOperations:
    def __init__(self, bundle, target, client_data, output):
        self.bundle, self.target, self.client_data, self.output = bundle, target, client_data, output
        self.python = target / 'venv/bin/python'
        self.config_path = target / 'config.toml'
        self.environment = {k: v for k, v in os.environ.items() if k not in
                            ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
        self.leases = []

    def save(self, receipt):
        (self.output / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')

    def preflight(self):
        from camctl.bootstrap.application import ConfigAdapter
        verify_checksums(self.bundle)
        arch = current_target()
        release = json.loads((self.bundle / 'RELEASE.json').read_text(encoding='utf-8'))
        expected_targets = wheel_target_manifest(self.bundle)
        if (release.get('wheel_targets') != expected_targets
                or any(not target['files'] for target in expected_targets.values())):
            raise ValueError('发行记录与离线依赖文件不一致')
        wheelhouse = self.bundle / 'camctl/wheelhouse' / arch
        if not wheelhouse.is_dir() or not list(wheelhouse.glob('*.whl')):
            raise ValueError('目标平台的离线依赖缺失: ' + str(wheelhouse))
        wheels = list((self.bundle / 'camctl').glob('camctl-*.whl'))
        if len(wheels) != 1 or not (self.bundle / 'camctl/requirements.txt').is_file():
            raise ValueError('camctl wheel 或锁定依赖文件缺失')
        with zipfile.ZipFile(wheels[0]) as archive:
            names = archive.namelist()
            metadata = [name for name in names if name.endswith('.dist-info/METADATA')]
            if len(metadata) != 1 or 'camctl/deployment.py' not in names:
                raise ValueError('camctl wheel 缺少发行元信息或只读检查入口')
            wheel_info = BytesParser().parsebytes(archive.read(metadata[0]))
            if (wheel_info['Name'] != 'camctl'
                    or wheel_info['Version'] != release['components']['camctl']):
                raise ValueError('camctl wheel 版本与发行记录不一致')
        if not self.config_path.is_file():
            raise ValueError('原配置不存在: ' + str(self.config_path))
        config = ConfigAdapter(Path.home()).load(self.config_path)
        state_db = Path(config.paths.state_db).resolve()
        if not state_db.is_file():
            raise ValueError('原状态库不存在或不是普通文件: ' + str(state_db))
        self.config, self.state_db, self.wheelhouse, self.wheel = config, state_db, wheelhouse, wheels[0]
        self.expected_version = release['components']['camctl']
        return {'state_db': str(state_db), 'config': str(self.config_path),
                'config_sha256': hashlib.sha256(self.config_path.read_bytes()).hexdigest(),
                'old_version': importlib.metadata.version('camctl'), 'new_version': self.expected_version,
                'release_source_sha256': release['source_sha256'],
                'bundle_manifest_sha256': hashlib.sha256((self.bundle / 'SHA256SUMS').read_bytes()).hexdigest()}

    def lock(self, original):
        from camctl.session.locks import acquire_admission, acquire_session, lock_file_paths
        session, admission = lock_file_paths(self.state_db)
        self.leases.append(acquire_session(session))
        try:
            self.leases.append(acquire_admission(admission))
        except BaseException:
            self.leases.pop().close()
            raise

    def unlock(self):
        errors = []
        while self.leases:
            try:
                self.leases.pop().close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup('释放更新锁失败', errors)

    def check_original(self, original):
        from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing, verify_directory_binding
        owned = open_existing(self.state_db, DbOpenMode.EXISTING_RO,
                              DbConfig(busy_timeout_ms=self.config.database.busy_timeout_ms))
        try:
            verify_directory_binding(owned.metadata, self.config.paths.staging, self.config.paths.ready,
                                     self.config.paths.processing)
            original['deployment'] = {'state_db': str(self.state_db), 'metadata': asdict(owned.metadata)}
        finally:
            owned.connection.close()

    def run(self, receipt, label, argv, output=None):
        command = list(map(str, argv))
        with (self.output / 'update.log').open('ab') as log:
            log.write(('\n$ ' + shlex.join(command) + '\n').encode()); log.flush()
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=log, env=self.environment)
            log.write(result.stdout)
        receipt['steps'].append({'label': label, 'argv': command, 'returncode': result.returncode})
        self.save(receipt)
        if result.returncode:
            raise RuntimeError(label + '失败，详见 update.log')
        if output is not None:
            output.write_bytes(result.stdout)
        return result.stdout.decode('utf-8')

    def install(self, receipt):
        common = ['uv', 'pip', 'install', '--python', self.python, '--offline', '--no-index',
                  '--find-links', self.wheelhouse]
        self.run(receipt, '离线安装锁定依赖', [*common, '--require-hashes', '-r', self.bundle / 'camctl/requirements.txt'])
        self.run(receipt, '离线安装 camctl', [*common, '--no-deps', '--reinstall-package', 'camctl', self.wheel])
        self.run(receipt, '检查依赖', ['uv', 'pip', 'check', '--python', self.python])

    def check_new(self, receipt):
        original = receipt['original']
        version = self.run(receipt, '检查发行版本', [self.target / 'venv/bin/camctl', '--version'])
        if version != self.expected_version + '\n':
            raise ValueError('发行版本不符: ' + repr(version))
        text = self.run(receipt, '只读检查原部署', [self.python, '-m', 'camctl.deployment', '--config', self.config_path])
        if json.loads(text) != original['deployment']:
            raise ValueError('状态库身份、格式或路径绑定与更新前不一致')
        if hashlib.sha256(self.config_path.read_bytes()).hexdigest() != original['config_sha256']:
            raise ValueError('原配置内容已变化')
        capability = self.output / 'device-capabilities.json'
        self.run(receipt, '导出原配置能力说明', [self.target / 'venv/bin/camctl', 'describe', '--config', self.config_path], output=capability)
        if self.client_data is not None:
            self.client_data.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(capability, self.client_data / 'device-capabilities.json')
            receipt['client_capabilities'] = str(self.client_data / 'device-capabilities.json')
        receipt['capabilities'] = str(capability)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--client-data', type=Path)
    args = parser.parse_args()
    if not args.target.is_absolute() or not args.target.is_dir():
        parser.error('--target 必须是已存在的绝对目录')
    if args.client_data is not None and not args.client_data.is_absolute():
        parser.error('--client-data 必须是绝对路径')
    args.target = Path(os.path.abspath(args.target))
    python = args.target / 'venv/bin/python'
    if not python.is_file() or shutil.which('uv') is None:
        parser.error('原 Python 环境或 uv 缺失')
    if os.path.abspath(sys.executable) != str(python):
        environment = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
        os.execve(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]], environment)
    if sys.version_info[:2] != (3, 11) or sys.implementation.name != 'cpython':
        parser.error('原环境必须使用 CPython 3.11')
    output = args.target / 'updates' / uuid.uuid4().hex
    output.mkdir(parents=True)
    (output / 'update.log').touch()
    receipt = perform_update(UpdateOperations(Path(__file__).resolve().parent, args.target, args.client_data, output))
    print('更新收据和日志目录：' + str(output))
    if receipt['status'] != 'updated_stopped':
        consequence = ('Python 环境可能已改变' if receipt['environment_may_have_changed']
                       else '尚未开始修改 Python 环境')
        print('更新失败：' + str(receipt.get('error', receipt.get('unlock_error'))) +
              '；' + consequence + '，请查看收据。', file=sys.stderr)
        return 1
    print('更新完成，host 保持停止；请按原入口恢复。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
