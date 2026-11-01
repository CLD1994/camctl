"""从 wheel 在虚拟环境外发布与部署绑定的稳定维护入口。"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import types
import zipfile

from camctl.operations.error_diagnostics import exception_diagnostic
from camctl.update_engine.diagnostic_fallback import freeze
from camctl.update_engine.layout import database_locator, environment_locator
from camctl.update_engine.log_registry import register as register_log_location


def _module_from_wheel(archive, name):
    module = types.ModuleType(name)
    source = archive.read('camctl/update_engine/' + name + '.py')
    exec(compile(source, name + '.py', 'exec'), module.__dict__)
    return module


def _publish_file(path, content, mode):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode)
        created = True
    except FileExistsError:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode or path.read_bytes() != content):
            raise ValueError('已存在的维护入口材料无法核实: ' + str(path))
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        created = False
    try:
        if created:
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(content)
                stream.flush()
            os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _launcher(base_python, router):
    return ('#!/bin/sh\nexec ' + shlex.quote(str(base_python)) + ' -B -I ' +
            shlex.quote(str(router)) + ' "$@"\n').encode()


def _publish_launcher(path, content, base_python, directory, binding):
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('既有维护入口无法核实')
        previous = path.read_bytes()
        if previous == content:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return
        try:
            lines = previous.decode('utf-8').splitlines()
            parts = shlex.split(lines[1])
            router = Path(parts[4])
            if (len(lines) != 2 or len(parts) != 6 or parts[:4] != ['exec', str(base_python), '-B', '-I']
                    or parts[5] != '$@' or previous != _launcher(base_python, router)
                    or (router != directory / 'router.py' and
                        not (router.name == 'router.py' and router.parent.parent == directory / 'versions'))):
                raise ValueError('既有维护入口不属于当前部署')
            old_binding = json.loads((router.parent / 'binding.json').read_text(encoding='utf-8'))
            if any(old_binding.get(key) != binding[key] for key in ('target', 'python', 'camctl_path')):
                raise ValueError('既有维护入口绑定不一致')
        except (IndexError, UnicodeError, ValueError, OSError) as error:
            raise ValueError('既有维护入口无法核实') from error
    fd, name = tempfile.mkstemp(prefix='.camctl-updater-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o700)
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def publish(wheel, target, base_python, camctl_path, config):
    target = Path(target)
    if not target.is_absolute():
        raise ValueError('目标必须使用绝对路径')
    lock_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _publish_locked(wheel, target, base_python, camctl_path, config)
    finally:
        os.close(lock_fd)


def _publish_locked(wheel, target, base_python, camctl_path, config):
    if any(not Path(value).is_absolute() for value in (wheel, target, base_python, camctl_path)):
        raise ValueError('wheel、目标、解释器和普通入口必须使用绝对路径')
    if config is not None and not Path(config).is_absolute():
        raise ValueError('配置路径必须是绝对路径')
    target = Path(target).resolve()
    directory = target / 'maintenance'
    if not Path(camctl_path).samefile(target / 'venv/bin/camctl'):
        raise ValueError('普通入口与部署目标不一致')
    interpreter = subprocess.run([str(base_python), '-B', '-I', '-c',
        'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) and '
        'sys.implementation.name == "cpython" and sys.prefix == sys.base_prefix else 1)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    if interpreter.returncode:
        raise ValueError('维护入口需要虚拟环境外的 CPython 3.11')
    with zipfile.ZipFile(wheel) as archive:
        transaction = _module_from_wheel(archive, 'update_transaction')
        router = archive.read('camctl/update_engine/router.py')
        result_schema = archive.read('camctl/_resources/protocol/software-update-result.schema.json')
        diagnostic_source = archive.read('camctl/operations/error_diagnostics.py')
        fallback_source = archive.read('camctl/update_engine/diagnostic_fallback.py')
        archive_source = archive.read('camctl/update_engine/archive_support.py')
        bundle_source = archive.read('camctl/update_engine/bundle_support.py')
        force_record_source = archive.read('camctl/update_engine/force_record.py')
        config_paths_source = archive.read('camctl/config_paths.py')
        transaction_source = archive.read('camctl/update_engine/update_transaction.py')
        pending = target / 'updates/pending.json'
        try:
            pending_info = pending.lstat()
        except FileNotFoundError:
            pending_info = None
        if pending_info is not None:
            if not stat.S_ISREG(pending_info.st_mode):
                raise ValueError('更新记录不是普通文件')
            record = json.loads(pending.read_text(encoding='utf-8'))
            state = transaction.classify_record(record)
            transaction.validate_record_target(record, target)
            if state not in (transaction.JournalState.COMMITTED, transaction.JournalState.ROLLED_BACK):
                raise ValueError('存在未完成更新，不能发布维护入口')
            transaction.sync_file(pending)
            transaction.sync_dir(pending.parent)
        import io
        with zipfile.ZipFile(io.BytesIO(archive.read('camctl/update_engine.pyz'))) as engine:
            probe = engine.read('probe.py').decode('utf-8')
    environment = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
    check = subprocess.run([str(target / 'venv/bin/python'), '-B', '-I', '-c', probe,
                            config or '', 'inspect'], capture_output=True, env=environment, check=False)
    if check.returncode:
        raise ValueError('原环境配置检查失败: ' + check.stderr.decode('utf-8', errors='replace'))
    original = json.loads(check.stdout)
    original['config_snapshot'] = transaction.snapshot_config(Path(original['config']), config)
    output = target / 'updates' / ('publish-' + os.urandom(16).hex())
    layout_path = directory / 'layout.json'
    layout = {'environment': str((target / 'venv').resolve()), 'state_db': original['state_db'],
              **{name: os.path.abspath(original['paths'][name]) for name in ('staging', 'ready', 'processing')}}
    locator_content = (json.dumps(str(layout_path), ensure_ascii=False) + '\n').encode()
    env_locator = environment_locator(target / 'venv')
    db_locator = database_locator(original['state_db'])
    binding = {'target': str(target), 'python': str(base_python), 'camctl_path': str(camctl_path),
               'layout_path': str(layout_path)}
    binding_bytes = (json.dumps(binding, ensure_ascii=False, sort_keys=True) + '\n').encode()
    files = {'router.py': (router, 0o644), 'result-schema.json': (result_schema, 0o644),
             'update_transaction.py': (transaction_source, 0o644), 'error_diagnostics.py': (diagnostic_source, 0o644),
             'diagnostic_fallback.py': (fallback_source, 0o644), 'archive_support.py': (archive_source, 0o644),
             'bundle_support.py': (bundle_source, 0o644), 'force_record.py': (force_record_source, 0o644),
             'config_paths.py': (config_paths_source, 0o644),
             'binding.json': (binding_bytes, 0o600)}
    version_hash = hashlib.sha256()
    for name, (content, mode) in sorted(files.items()):
        version_hash.update(name.encode() + b'\0' + str(mode).encode() + b'\0')
        version_hash.update(hashlib.sha256(content).digest())
    versions = directory / 'versions'
    version_dir = versions / version_hash.hexdigest()
    launcher = _launcher(base_python, version_dir / 'router.py')
    layout_bytes = (json.dumps(layout, ensure_ascii=False, sort_keys=True) + '\n').encode()
    transaction.validate_paths(target, original, None, output,
        [original['paths'][name] for name in ('staging', 'ready', 'processing')], before_lock=True,
        extra_effect_dirs=(directory,), extra_control_files=(layout_path, directory / 'camctl-updater',
            *tuple(version_dir / name for name in files), env_locator, db_locator))
    for path in (directory, versions, version_dir):
        try:
            path.mkdir(mode=0o700)
            transaction.sync_dir(path.parent)
        except FileExistsError:
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError('已存在的维护目录无法核实: ' + str(path))
    _publish_file(layout_path, layout_bytes, 0o600)
    transaction.sync_dir(directory)
    for name, (content, mode) in files.items():
        _publish_file(version_dir / name, content, mode)
        transaction.sync_dir(version_dir)
    for locator in (env_locator, db_locator):
        _publish_file(locator, locator_content, 0o600)
        transaction.sync_dir(locator.parent)
    register_log_location(original['log_file'], config, environment=target / 'venv')
    _publish_launcher(directory / 'camctl-updater', launcher, base_python, directory, binding)
    transaction.sync_dir(directory)
    return directory / 'camctl-updater'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--base-python', required=True)
    parser.add_argument('--camctl-path', required=True)
    parser.add_argument('--config')
    args = parser.parse_args()
    try:
        print(publish(args.wheel, args.target, args.base_python, args.camctl_path, args.config))
    except Exception as error:
        try:
            diagnostic = freeze(error, 'publish', exception_diagnostic)
            print(json.dumps(diagnostic, ensure_ascii=False, default=repr), file=sys.stderr)
            sys.stderr.flush()
        except Exception:
            pass
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
