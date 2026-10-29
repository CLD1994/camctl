"""同一冻结输入的诊断；不替代原测试，不修改项目文件。"""
from pathlib import Path
import ast
import asyncio
import collections
from contextlib import closing
from dataclasses import asdict, replace
import gc
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import sys
import time
import tracemalloc
import cProfile
import pstats

RUN = Path(__file__).resolve().parent
META = json.loads((RUN / 'metadata.json').read_text())
SOURCE = Path(META['source'])
sys.path.insert(0, str(SOURCE / 'apps/camctl/scripts'))
import sync_resources
sync_resources.sync_package_resources(SOURCE / 'apps/camctl')
from integration.persistence import test_segment_transaction_batches as test
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.contracts.values import new_operation_key
from camctl.logging_runtime import transaction_timing as timing
import camctl

def event(kind, **fields):
    row = {'utc': time.time(), 'kind': kind, **fields}
    with (RUN / 'progress.jsonl').open('a') as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + '\n')

def digest(db):
    return hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()

# 原样读取原测试的两段局部类定义，不另行维护其行为。
test_path = Path(test.__file__)
node = next(n for n in ast.parse(test_path.read_text()).body
            if isinstance(n, ast.AsyncFunctionDef) and n.name ==
            'test_fixed_batch_write_and_verification_have_bounded_residency')
classes = [n for n in node.body if isinstance(n, ast.ClassDef)]
namespace = {}
exec(compile(ast.Module(body=classes, type_ignores=[]), str(test_path), 'exec'), namespace)
MeasuredConnection = namespace['MeasuredConnection']

event('prepare_start', N=META['N'])
start = time.perf_counter()
owned, runtime, repo, ref, request = asyncio.run(test._ready_world(RUN / 'world', count=META['N']))
dbpath = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
metadata = owned.metadata
baseline_dump = digest(owned.connection)
next_tx = owned.connection.execute('SELECT MAX(id)+1 FROM history_transactions').fetchone()[0]
baseline_pragmas = {key: owned.connection.execute('PRAGMA ' + key).fetchone()[0]
                    for key in ('journal_mode', 'synchronous', 'cache_size', 'wal_autocheckpoint')}
owned.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
owned.connection.close()
assert not Path(str(dbpath) + '-wal').exists()
assert not Path(str(dbpath) + '-shm').exists()
frozen = RUN / 'frozen-state.db'
shutil.copy2(dbpath, frozen)
frozen_sha = hashlib.sha256(frozen.read_bytes()).hexdigest()
event('prepare_end', wall_s=time.perf_counter() - start, database=str(dbpath),
      frozen_sha256=frozen_sha, logical_sha256=baseline_dump)

key = new_operation_key()
results = []
reference_result = None
reference_after = None

def operation(label, function, traced, *args, **kwargs):
    event('operation_start', label=label, traced=traced)
    gc.collect()
    if traced:
        tracemalloc.start()
        tracemalloc.reset_peak()
    wall, cpu = time.perf_counter(), time.process_time()
    try:
        value = function(*args, **kwargs)
    finally:
        elapsed, cpu_elapsed = time.perf_counter() - wall, time.process_time() - cpu
        peak = tracemalloc.get_traced_memory()[1] if traced else None
        if traced:
            tracemalloc.stop()
        event('operation_end', label=label, wall_s=elapsed, process_cpu_s=cpu_elapsed, peak_bytes=peak)
    return value, {'wall_s': elapsed, 'process_cpu_s': cpu_elapsed, 'peak_bytes': peak}

def restore():
    assert not Path(str(dbpath) + '-wal').exists()
    assert not Path(str(dbpath) + '-shm').exists()
    shutil.copy2(frozen, dbpath)
    assert hashlib.sha256(dbpath.read_bytes()).hexdigest() == frozen_sha
    current = open_existing(dbpath, DbOpenMode.EXISTING_RW, DbConfig())
    assert current.metadata == metadata
    assert digest(current.connection) == baseline_dump
    pragmas = {name: current.connection.execute('PRAGMA ' + name).fetchone()[0]
               for name in baseline_pragmas}
    assert pragmas == baseline_pragmas
    return current

for name, counter, traced in (
    ('plain', False, False),
    ('original_counter_and_tracemalloc', True, True),
    ('original_counter_only', True, False),
    ('tracemalloc_only', False, True),
):
    current = restore()
    try:
        stats = {'max_fetch': 0, 'fetched_rows': 0, 'sql_calls': 0, 'whole_group_checks': 0}
        measured = replace(current, connection=MeasuredConnection(current.connection, stats, next_tx)) if counter else current
        saved, write = operation(name + '/write', test.transactions.finish_segment_recording,
                                 traced, request, key, measured, batch_size=META['B'])
        assert saved.kind == 'completed', saved.error
        after_write = dict(stats)
        verified, verify = operation(name + '/verify', test.transactions.verify_segment_finish,
                                    traced, request, key, measured, execution_finished=True, batch_size=META['B'])
        assert verified == saved.result and verified.output_count == META['N']
        if counter:
            assert stats['max_fetch'] <= 128 and stats['whole_group_checks'] <= 3
        after_dump = digest(current.connection)
        if reference_result is None:
            reference_result, reference_after = saved.result, after_dump
        assert saved.result == reference_result
        assert after_dump == reference_after
        result = {'mode': name, 'sql_probe': counter, 'tracemalloc': traced,
                  'write': write, 'verify': verify, 'combined_wall_s': write['wall_s'] + verify['wall_s'],
                  'stats_after_write': after_write if counter else None,
                  'stats_after_verify': stats if counter else None,
                  'after_logical_sha256': after_dump, 'result': asdict(saved.result)}
        results.append(result)
        (RUN / 'factor-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str) + '\n')
    finally:
        current.connection.close()

# 另做一遍带详细观察的诊断。此耗时不参与四因素对照。
sql_counts = collections.Counter()
sql_times = collections.Counter()
measurement = None
class DiagnosticConnection:
    def __init__(self, db): self.db = db
    def __getattr__(self, name): return getattr(self.db, name)
    def execute(self, sql, params=()):
        group = (measurement.phase if measurement is not None else 'outside', sql)
        started = time.perf_counter()
        try: return self.db.execute(sql, params)
        finally:
            sql_counts[group] += 1
            sql_times[group] += time.perf_counter() - started

current = restore()
try:
    assert not timing.performance_enabled(), '避免外层计量覆盖本次显式诊断'
    measurement = timing._Measurement()
    emitted = []
    original_record = timing.record_performance
    timing.record_performance = lambda stage, elapsed, **fields: emitted.append({'stage': stage, 'wall_ns': elapsed, **fields})
    token = timing._current.set(measurement)
    profile = cProfile.Profile()
    try:
        event('diagnostic_start')
        profile.enable()
        saved = test.transactions.finish_segment_recording(request, key,
            replace(current, connection=DiagnosticConnection(current.connection)), batch_size=META['B'])
        profile.disable()
        assert saved.kind == 'completed', saved.error
        measurement.emit()
    finally:
        profile.disable()
        timing._current.reset(token)
        timing.record_performance = original_record
    assert saved.result == reference_result
    assert digest(current.connection) == reference_after
    profile.dump_stats(str(RUN / 'write-profile.pstats'))
    stats = pstats.Stats(profile)
    entries = []
    for (filename, line, function), (primitive, total, own, cumulative, callers) in stats.stats.items():
        entries.append({'file': filename, 'line': line, 'function': function,
                        'primitive_calls': primitive, 'calls': total,
                        'self_seconds': own, 'cumulative_seconds': cumulative})
    (RUN / 'write-profile-functions.json').write_text(json.dumps(sorted(entries, key=lambda row: -row['cumulative_seconds']), ensure_ascii=False, indent=2) + '\n')
    (RUN / 'write-transaction-stages.json').write_text(json.dumps(emitted, ensure_ascii=False, indent=2) + '\n')
    sql = [{'phase': phase, 'sql': sql, 'calls': count, 'execute_wall_s': sql_times[(phase, sql)]}
           for (phase, sql), count in sql_counts.most_common()]
    (RUN / 'write-sql-groups.json').write_text(json.dumps(sql, ensure_ascii=False, indent=2) + '\n')
    event('diagnostic_end', statement_calls=sum(sql_counts.values()), after_logical_sha256=reference_after)
finally:
    current.connection.close()

META.update(python=platform.python_version(), sqlite=sqlite3.sqlite_version,
            executable=sys.executable, camctl_file=camctl.__file__, test_file=str(test_path),
            test_sha256=hashlib.sha256(test_path.read_bytes()).hexdigest(), frozen_sha256=frozen_sha,
            baseline_logical_sha256=baseline_dump, result_logical_sha256=reference_after,
            database_path=str(dbpath), pragmas=baseline_pragmas,
            note='四档同一物理路径恢复同一关闭后WAL数据库、同一请求与操作键；每档结果和完整逻辑数据一致。单次顺序测量，未重置OS缓存。诊断档另带cProfile与SQL/阶段观察，不用于性能对照。')
(RUN / 'metadata.json').write_text(json.dumps(META, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'factor_results': results, 'source_head': META['source_head']}, ensure_ascii=False, default=str))
