"""只验证真实 Action6 清理测试 helper 的控制边界；不启动 C 或生产流程。"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time as real_time
from types import SimpleNamespace
import traceback
from unittest.mock import create_autospec

ROOT = Path('/tmp/camctl-quality-repair-lidlmje7/osmo-recovery')
EVIDENCE = Path(__file__).parent
COMMIT = 'bb72d223b4641eb9b1c7da945eed1295837aeb93'
SOURCE_PATH = 'tests/integration/test_action6_segmented_recovery_roundtrip.py'
for relative in ['apps/camctl/src', 'apps/camctl/tests', 'tests/integration', 'scripts']:
    sys.path.insert(0, str(ROOT / relative))
import _wsl_host_demo as host
from camctl_fixtures import Deployment
from camctl.contracts.enums import enum_for

source = subprocess.check_output(['git', 'show', COMMIT + ':' + SOURCE_PATH], cwd=ROOT, text=True)
(EVIDENCE / 'original-test-module.py').write_text(source)
module = ast.parse(source)
node = next(item for item in module.body if isinstance(item, ast.FunctionDef) and item.name == '_cleanup_inspection_inputs')
helper_text = ast.get_source_segment(source, node) + '\n'
(EVIDENCE / 'original-helper.py').write_text(helper_text)
records_source = subprocess.check_output(['git', 'show', COMMIT + ':tests/integration/test_osmo_builtin_timelapse_roundtrip.py'], cwd=ROOT, text=True)
records_node = next(item for item in ast.parse(records_source).body if isinstance(item, ast.FunctionDef) and item.name == '_records')
original_wait = host.wait_for
original_query = host.query_state
original_host_time = host.time
PURPOSE = int(enum_for('intermediate_files.purpose').RECORDING_INPUT)
RELEASED = int(enum_for('intermediate_files.retention_state').RELEASABLE)
COMPLETED = int(enum_for('intermediate_files.cleanup_state').COMPLETED)
RETAINED = int(enum_for('intermediate_files.retention_state').REQUIRED)
PENDING = int(enum_for('intermediate_files.cleanup_state').PENDING)
FILE_SQL = ('SELECT id,relative_path,retention_state,cleanup_state FROM intermediate_files '
            'WHERE owner_action_id=? AND purpose=? ORDER BY id')
HISTORY_SQL = 'SELECT id,body_json FROM history_events WHERE id>? ORDER BY id'


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.last = None
    def monotonic(self):
        value = self.now
        self.last = value
        self.now += 1.0
        return value


def run_case(name, expected_message=None, *, expected_error=AssertionError, restarts=0):
    directory = EVIDENCE / 'case-runs' / str(os.getpid()) / name
    deployment = Deployment(directory, devices=False)
    clock = Clock()
    waits, events, queries = [], [], []
    old_pid, new_pid = 42001, 42002
    run_log = deployment.root / 'cleanup-runs.jsonl'
    old_run = {'pid': old_pid, 'history_horizon': 100, 'started_monotonic_ns': 1}
    if name != 'no_run_record':
        run_log.write_text(json.dumps(old_run) + '\n')
    db = sqlite3.connect(deployment.state_db)
    db.executescript('CREATE TABLE intermediate_files(id INTEGER PRIMARY KEY,relative_path TEXT,'
                     'retention_state INTEGER,cleanup_state INTEGER,owner_action_id INTEGER,purpose INTEGER);'
                     'CREATE TABLE history_events(id INTEGER PRIMARY KEY,body_json TEXT);')
    for identity in [1, 2]:
        db.execute('INSERT INTO intermediate_files VALUES(?,?,?,?,?,?)',
                   (identity, f'input-{identity}.mp4', RETAINED, PENDING, 7, PURPOSE))
        (deployment.staging / f'input-{identity}.mp4').write_bytes(b'controlled helper input')
    for identity in [1, 2]:
        if name == 'partial_cursor' and identity == 2:
            continue
        event_id = 98 + identity if name == 'stale_cursor' else 100 + identity
        table = 'other_table' if name == 'wrong_cursor_table' else 'runtime_state'
        body = {'rows': [{'table': table, 'after': {'values': {'cleanup_cursor_file_id': identity}}}]}
        db.execute('INSERT INTO history_events VALUES(?,?)', (event_id, json.dumps(body)))
    db.commit()
    started = False
    next_announced = False
    deleted = False
    delayed = name in {'shared_budget_success', 'shared_budget_exhausted'}
    cursor_at = 1020 if delayed else 1000
    exit_at = 1060 if delayed else 1000
    gone_at = 1100 if delayed else 1000
    next_at = 1120 if delayed else 1000
    deleted_at = (1190 if name == 'shared_budget_exhausted' else 1140) if delayed else 1000

    def complete_files():
        nonlocal deleted
        if deleted:
            return
        deleted = True
        if name == 'file_id_changed':
            db.execute('UPDATE intermediate_files SET id=id+10')
        if name == 'file_path_changed':
            db.execute("UPDATE intermediate_files SET relative_path='changed-'||relative_path")
        db.execute('UPDATE intermediate_files SET retention_state=?,cleanup_state=?', (RELEASED, COMPLETED))
        db.commit()
        if name != 'file_still_exists':
            for path in deployment.staging.iterdir():
                path.unlink()
        events.append({'event': 'controlled_cleanup_result', 'at': clock.now})

    if name == 'already_completed':
        complete_files()

    def records(path):
        nonlocal next_announced
        assert path == run_log
        if started and not next_announced and clock.now >= next_at:
            next_announced = True
            pid = old_pid if name == 'same_next_pid' else new_pid
            with run_log.open('a') as stream:
                stream.write(json.dumps({'pid': pid, 'history_horizon': 102, 'started_monotonic_ns': 2}) + '\n')
        return real_records(path)

    def query(state_db, sql, params=()):
        assert state_db == deployment.state_db
        assert (sql, params) in {(FILE_SQL, (7, PURPOSE)), (HISTORY_SQL, (100,))}
        queries.append({'sql': sql, 'params': params, 'at': clock.now})
        if sql == FILE_SQL and started and name != 'next_run_no_cleanup' and clock.now >= deleted_at:
            complete_files()
        result = original_query(state_db, sql, params)
        if sql == HISTORY_SQL and clock.now < cursor_at:
            return []
        return result

    def wait(predicate, timeout_s, message, *, failure=None):
        # timeout 参数必须来自原helper同一个deadline；不替换真实轮询或断言。
        assert timeout_s == max(0, 1180.0 - clock.last)
        row = {'message': message, 'timeout_seconds': timeout_s, 'at': clock.now}
        waits.append(row)
        try:
            return original_wait(predicate, timeout_s, message, failure=failure)
        finally:
            row['finished_at'] = clock.now

    def module_log():
        events.append({'event': 'observe_old_result', 'at': clock.now})
        if name in {'old_not_success', 'failure_hook'} or clock.now < exit_at:
            return ''
        return f'call=1 command=run pid={old_pid} result=success exit_kind=0 exit=0 needs_run=0\n'

    def killpg(pid, signal):
        assert (pid, signal) == (old_pid, 0)
        events.append({'event': 'observe_old_group', 'at': clock.now})
        if name == 'group_permission_error':
            raise PermissionError('group permission denied')
        if name == 'old_group_alive' or clock.now < gone_at:
            return
        raise ProcessLookupError()

    def stop():
        assert clock.now >= exit_at and name != 'old_not_success'
        assert clock.now >= gone_at and name not in {'old_group_alive', 'group_permission_error'}
        events.append({'event': 'stop_old_host_after_natural_exit', 'at': clock.now})

    def start(*, env=None, use_default_paths=False):
        nonlocal started
        assert env == {'controlled': name} and not use_default_paths
        started = True
        events.append({'event': 'start_next_normal_run', 'at': clock.now})

    demo = create_autospec(host.HostDemo, instance=True, spec_set=True)
    demo.module_log.side_effect = module_log
    demo.stop.side_effect = stop
    demo.start.side_effect = start
    demo.watch_failures.return_value = lambda: None
    failure = (lambda: 'controlled original run failure') if name == 'failure_hook' else (lambda: None)
    namespace = {'time': clock, 'os': SimpleNamespace(killpg=killpg), 're': re, 'json': json,
                 'enum_for': enum_for, 'query_state': query, 'wait_for': wait}
    exec(compile(ast.Module(body=[records_node], type_ignores=[]), SOURCE_PATH, 'exec'), namespace)
    real_records = namespace['_records']
    namespace['_records'] = records
    exec(compile(ast.Module(body=[node], type_ignores=[]), COMMIT + ':' + SOURCE_PATH, 'exec'), namespace)
    host.time = clock
    error = None
    try:
        namespace['_cleanup_inspection_inputs'](demo, {'deployment': deployment, 'environment': {'controlled': name}}, 7, failure)
    except BaseException as observed:
        error = observed
    finally:
        host.time = original_host_time
        db.close()
    if expected_message is None:
        assert error is None, repr(error)
    else:
        assert isinstance(error, expected_error), repr(error)
        assert expected_message in str(error), str(error)
        if expected_error is AssertionError:
            origin = traceback.extract_tb(error.__traceback__)[-1]
            assert origin.filename in {COMMIT + ':' + SOURCE_PATH, str(ROOT / 'tests/integration/_wsl_host_demo.py')}, origin
    assert demo.stop.call_count == restarts and demo.start.call_count == restarts
    demo.submit.assert_not_called()
    if name == 'shared_budget_success':
        assert len(waits) == 5 and waits[-1]['timeout_seconds'] < waits[0]['timeout_seconds'] - 100
        assert clock.now < 1180
    if name == 'shared_budget_exhausted':
        assert waits[-1]['timeout_seconds'] < 70 and clock.now < 1190
    receipt = {'case': name, 'outcome': 'passed', 'helper_error_type': type(error).__name__ if error else None,
               'helper_error': str(error) if error else None,
               'helper_error_origin': ({'filename': origin.filename, 'line': origin.lineno, 'function': origin.name} if error is not None and expected_error is AssertionError else None), 'waits': waits, 'events': events,
               'query_count': len(queries), 'stop_count': demo.stop.call_count, 'start_count': demo.start.call_count,
               'submit_count': demo.submit.call_count, 'virtual_elapsed_seconds': clock.now - 1000}
    (directory / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: receipt[k] for k in ['case', 'outcome', 'helper_error_type', 'helper_error', 'stop_count', 'start_count', 'submit_count']}, ensure_ascii=False))
    return receipt


cases = [
    ('already_completed', None, 0), ('keep_then_next_run', None, 1), ('shared_budget_success', None, 1),
    ('no_run_record', '未完成清理或本轮检查', 0), ('stale_cursor', '未完成清理或本轮检查', 0),
    ('partial_cursor', '未完成清理或本轮检查', 0), ('wrong_cursor_table', '未完成清理或本轮检查', 0),
    ('old_not_success', '自然结束', 0), ('old_group_alive', '执行组尚未收场', 0),
    ('same_next_pid', '下一次正常 run', 1), ('next_run_no_cleanup', '仍未完成清理', 1),
    ('file_id_changed', '', 1), ('file_path_changed', '', 1), ('file_still_exists', '', 1),
    ('shared_budget_exhausted', '仍未完成清理', 1), ('failure_hook', 'controlled original run failure', 0),
]
started_at = real_time.time()
results = [run_case(name, message, restarts=count) for name, message, count in cases]
results.append(run_case('group_permission_error', 'group permission denied', expected_error=PermissionError))
summary = {'source_commit': COMMIT, 'source_path': SOURCE_PATH, 'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
           'helper_sha256': hashlib.sha256(helper_text.encode()).hexdigest(), 'real_helper_ast_executed': True,
           'real_query_state_and_wait_for_used': True, 'real_deployment_and_files_used': True,
           'demo_spec': 'unittest.mock.create_autospec(HostDemo, instance=True, spec_set=True)',
           'cases': len(results), 'passed': len(results), 'results': results,
           'wall_seconds': real_time.time() - started_at, 'boundary': 'External helper control validation with controlled outcomes. No production state machine, real C restart, CLI or Action6 chain was run. Real C restart and SQL semantic evidence remain in the separately completed integration run and cleanup-followup witness.'}
(EVIDENCE / 'result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({key: summary[key] for key in ['cases', 'passed', 'wall_seconds', 'source_commit', 'helper_sha256', 'boundary']}, ensure_ascii=False))
