"""部署外稳定入口：维护资格检查、冻结引擎恢复与新事务路由。"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).parent))
import update_transaction as transaction
from error_diagnostics import exception_diagnostic


def _diagnose(error, stage):
    try:
        diagnostic = exception_diagnostic(error)
        diagnostic['stage'] = stage
        sys.stderr.write(json.dumps(diagnostic, ensure_ascii=False, default=repr) + '\n')
        sys.stderr.flush()
    except Exception:
        # 诊断通道故障不能阻挡锁释放或撤销已经确认的结果。
        pass


def _read_result(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('维护引擎结果存在重复字段')
            result[key] = value
        return result
    result = json.loads(data, object_pairs_hook=unique)
    if not isinstance(result, dict) or not isinstance(result.get('status'), str):
        raise ValueError('维护引擎结果无效')
    return result


class LockBusy(Exception):
    pass


def _lock(target):
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(fd)
        raise LockBusy('维护资格被占用') from error
    except BaseException:
        os.close(fd)
        raise
    return fd


def _pending(target):
    path = target / 'updates/pending.json'
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('更新记录不是普通文件')
    record = json.loads(path.read_text(encoding='utf-8'))
    state = transaction.classify_record(record)
    transaction.validate_record_target(record, target)
    if transaction.directory_identity(Path(record['output'])) != record['output_identity']:
        raise ValueError('更新输出目录身份已变化')
    if state in (transaction.JournalState.COMMITTED, transaction.JournalState.ROLLED_BACK):
        transaction.sync_file(path)
        transaction.sync_dir(path.parent)
    return record, state


def _invoke(python, engine, lock_fd, args, extra_fds=()):
    command = [python, '-B', '-I', str(engine), *args, '--lock-fd', str(lock_fd)]
    try:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, pass_fds=(lock_fd, *extra_fds))
    finally:
        # 允许引擎在回滚前关闭其资格并重取，以确认继承锁的写入者结束。
        os.close(lock_fd)
    data, _ = child.communicate()
    result = _read_result(data)
    status = result['status']
    if child.returncode < 0 or (child.returncode == 0) != (status in ('updated_stopped', 'recovered', 'unchanged')):
        raise ValueError('维护引擎退出状态与结果不一致')
    if status not in ('updated_stopped', 'recovered', 'unchanged', 'failed'):
        raise ValueError('维护引擎结果状态无效')
    return result


def _frozen(record):
    engine = record.get('engine')
    expected = Path(record['output']) / 'engine.pyz'
    if (not isinstance(engine, dict) or set(engine) != {'path', 'sha256'}
            or engine['path'] != str(expected) or not isinstance(engine['sha256'], str)
            or len(engine['sha256']) != 64):
        raise ValueError('未完成事务的引擎定位无效')
    info = expected.lstat()
    if not stat.S_ISREG(info.st_mode) or transaction.digest(expected) != engine['sha256']:
        raise ValueError('未完成事务的冻结引擎无法核实')
    return expected


def _engine_memfd(bundle):
    import zipfile
    wheels = list((bundle / 'camctl').glob('camctl-*.whl'))
    if len(wheels) != 1:
        raise ValueError('更新包必须包含唯一 camctl wheel')
    with zipfile.ZipFile(wheels[0]) as wheel:
        data = wheel.read('camctl/update_engine.pyz')
    fd = os.memfd_create('camctl-update-engine', os.MFD_CLOEXEC)
    try:
        with os.fdopen(os.dup(fd), 'wb') as stream:
            stream.write(data)
            stream.flush()
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd


def execute(binding, action, camctl_path, config, bundle, client_data):
    result = dict(version=1, action=action, outcome='not_started', effect='not_started', eligibility='unknown',
                  prior_recovery='none', reason=None, stage=None, evidence=None)
    target = Path(binding['target'])
    python = binding['python']
    if camctl_path != binding['camctl_path']:
        result['reason'] = '普通入口与维护入口绑定不一致'
        return result
    lock_fd = None
    invoked = False
    qualification_proven = False
    known_active = False
    stage = 'lock'
    try:
        lock_fd = _lock(target)
        stage = 'pending'
        record, state = _pending(target)
        active = state in (transaction.JournalState.PREPARING, transaction.JournalState.UPDATING)
        known_active = active
        qualification_proven = not active
        if active:
            if action == 'check':
                result.update(outcome='recovery_required', eligibility='blocked', reason='存在未完成更新')
                return result
            if record['original'].get('config_source') != config:
                raise ValueError('维护配置来源与未完成事务不一致')
            stage = 'frozen_engine'
            frozen = _frozen(record)
            child_args = ['--target', str(target), '--recover-only']
            inherited_fd, lock_fd = lock_fd, None
            invoked = True
            stage = 'recover'
            child = _invoke(python, frozen, inherited_fd, child_args)
            if child['status'] != 'recovered':
                result.update(outcome='recovery_required', eligibility='blocked',
                              reason='未完成事务未能核实恢复', stage=child.get('stage'),
                              evidence=child.get('evidence'), prior_recovery='unknown')
                return result
            result['prior_recovery'] = 'recovered'
            known_active = False
            lock_fd = _lock(target)
            _, state = _pending(target)
            if state not in (transaction.JournalState.COMMITTED, transaction.JournalState.ROLLED_BACK):
                raise ValueError('恢复后更新记录不是可靠终态')
            qualification_proven = True
        if action in ('check', 'recover'):
            result.update(outcome='recovered' if active else 'unchanged',
                          effect='rolled_back' if active and record['mutation_intent'] else 'unchanged',
                          eligibility='allowed')
            return result
        if bundle is None:
            raise ValueError('应用更新需要交付目录')
        stage = 'read_new_engine'
        memfd = _engine_memfd(Path(bundle))
        try:
            output = target / 'updates' / uuid.uuid4().hex
            child_args = ['--target', str(target), '--bundle', bundle, '--output', str(output)]
            if config is not None:
                child_args.extend(('--config', config))
            if client_data is not None:
                child_args.extend(('--client-data', client_data))
            inherited_fd, lock_fd = lock_fd, None
            invoked = True
            qualification_proven = False
            stage = 'apply'
            child = _invoke(python, '/proc/self/fd/' + str(memfd), inherited_fd, child_args, (memfd,))
        finally:
            os.close(memfd)
        effect = child.get('effect')
        if ((child['status'] == 'updated_stopped' and effect != 'committed')
                or (child['status'] == 'failed' and effect not in
                    ('unchanged', 'rolled_back', 'recovery_required', 'unknown'))):
            raise ValueError('维护引擎请求结果与效果不一致')
        result.update(effect=effect, stage=child.get('stage'), evidence=child.get('evidence'))
        stage = 'confirm_result'
        lock_fd = _lock(target)
        current, state = _pending(target)
        if effect in ('committed', 'rolled_back') and (current is None or current['output'] != str(output)):
            raise ValueError('更新效果与本次事务记录不一致')
        if effect == 'rolled_back' and state is not transaction.JournalState.ROLLED_BACK:
            raise ValueError('回滚效果与持久记录不一致')
        if effect == 'recovery_required' and state not in (transaction.JournalState.PREPARING, transaction.JournalState.UPDATING):
            raise ValueError('恢复责任与持久记录不一致')
        if effect == 'unchanged' and state is transaction.JournalState.UPDATING:
            raise ValueError('未修改效果与持久记录不一致')
        if state in (transaction.JournalState.PREPARING, transaction.JournalState.UPDATING):
            result.update(outcome='recovery_required', eligibility='blocked', reason='存在未完成更新')
        elif child['status'] == 'updated_stopped' and state is transaction.JournalState.COMMITTED:
            result.update(outcome='applied', eligibility='allowed')
        elif child['status'] == 'failed':
            result.update(outcome='failed', eligibility='allowed')
        else:
            raise ValueError('更新结果与持久记录不一致')
    except LockBusy as error:
        _diagnose(error, stage)
        result.update(outcome='unknown' if invoked else 'not_started',
                      effect='unknown' if invoked else 'not_started', eligibility='blocked', reason='维护资格被占用')
    except Exception as error:
        _diagnose(error, stage)
        if qualification_proven and lock_fd is not None:
            result.update(outcome='failed', effect='unchanged', eligibility='allowed')
        elif known_active and not invoked:
            result.update(outcome='recovery_required', effect='not_started', eligibility='blocked')
        else:
            result.update(outcome='unknown', effect='unknown', eligibility='unknown')
        result['reason'] = f'{type(error).__name__}: {error}'
        result['stage'] = stage
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
    return result


def main():
    schema = json.loads((Path(__file__).parent / 'result-schema.json').read_text(encoding='utf-8'))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=schema['properties']['action']['enum'])
    parser.add_argument('--camctl-path', required=True)
    parser.add_argument('--config')
    parser.add_argument('--bundle')
    parser.add_argument('--client-data')
    args = parser.parse_args()
    for value in (args.camctl_path, args.config, args.bundle, args.client_data):
        if value is not None and (not Path(value).is_absolute() or '\0' in value):
            parser.error('路径必须是不含空字符的绝对路径')
    try:
        binding = json.loads((Path(__file__).parent / 'binding.json').read_text(encoding='utf-8'))
        result = execute(binding, args.action, args.camctl_path, args.config, args.bundle, args.client_data)
    except Exception as error:
        _diagnose(error, 'binding')
        result = dict(version=1, action=args.action, outcome='not_started', effect='not_started', eligibility='unknown',
                      prior_recovery='none', reason=f'{type(error).__name__}: {error}', stage=None, evidence=None)
    print(json.dumps(result, ensure_ascii=False))
    exits = schema['x-exit-status']
    return exits['success'] if result['eligibility'] == 'allowed' and result['outcome'] in exits['success_outcomes'] else exits['failure']


if __name__ == '__main__':
    raise SystemExit(main())
