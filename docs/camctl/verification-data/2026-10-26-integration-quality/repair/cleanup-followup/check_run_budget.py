"""从现场的独立 SQLite/文件副本核对生产中间文件维护生命周期。"""
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(sys.argv[1]).resolve()
EVIDENCE = Path(__file__).resolve().parent
SOURCE = Path('/tmp/camctl-quality-repair-evidence/final-action6/remaining/failed-source-commit-diagnostic/state.db')
sys.path[:0] = [str(REPO / 'apps/camctl/src'), str(REPO / 'apps/camctl/scripts')]
from sync_resources import sync_package_resources
sync_package_resources(REPO / 'apps/camctl')
from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.host_files.tasks import AsyncFileTask, FileTaskId
from camctl.outputs import work_files
from camctl.outputs.work_files import WorkFileLimits, WorkFileSingleOutcome, classify_work_file
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import OutputsRepository, register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

register_capture_guards()
register_operation_guards()
register_outputs_guards()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def duplicate(name):
    target = EVIDENCE / name
    attempt = 1
    while target.exists():
        attempt += 1
        target = EVIDENCE / (name + '-' + str(attempt))
    target.mkdir()
    with sqlite3.connect(SOURCE.as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(target / 'state.db') as destination:
            source.backup(destination)
        staging = Path(source.execute('SELECT staging_path FROM database_metadata').fetchone()[0])
        files = source.execute('SELECT id,relative_path FROM intermediate_files WHERE id IN (1,2,3) ORDER BY id').fetchall()
    for file_id, relative in files:
        local = target / 'staging' / relative
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staging / relative, local)
        assert digest(local) == digest(staging / relative)
    (target / 'staging' / 'recording-inputs' / 'unregistered.mp4').write_bytes(b'not a cleanup candidate')
    return target, staging, files


async def state(database):
    def read(owned):
        return {
            'boundary': owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0],
            'cursor': owned.connection.execute('SELECT cleanup_cursor_file_id FROM runtime_state').fetchone()[0],
            'files': owned.connection.execute('SELECT id,retention_state,cleanup_state FROM intermediate_files WHERE id IN (1,2,3) ORDER BY id').fetchall(),
            'classifications': [dict(facts=asdict(facts), decision=asdict(classify_work_file(facts)))
                                for facts in (OutputsRepository().load_work_file_state(i, owned) for i in (1,2,3))],
        }
    return await database.read(read)


async def one_round(runtime, context):
    await runtime.flow(context)
    await runtime.settle()
    assert runtime.pending_results == {}
    assert not runtime.executor.unfinished_files()


async def lifecycle():
    target, original_staging, files = duplicate('lifecycle')
    database = await BusinessDatabase.open(lambda: open_existing(target / 'state.db', DbOpenMode.EXISTING_RW, DbConfig()))
    context = SimpleNamespace(database=database, clock=SimpleNamespace(utc_micros=lambda: int(time.time()*1_000_000)))
    runtime = WorkFileRuntime(staging=target / 'staging', limits=WorkFileLimits(32, 128))
    await runtime.initialize(database)
    entered, release = asyncio.Event(), asyncio.Event()
    async def actual_use(_control):
        entered.set()
        await release.wait()
    owner = asyncio.create_task(runtime.executor.run_owned_async_file_task(AsyncFileTask(
        FileTaskId('diagnostic-real-file-use'), (1,2,3), 'read', '检查副本实际读取', actual_use)))
    await entered.wait()
    observed, removed = [], []
    observe, remove = work_files._observe_work_file, work_files._remove_work_file
    def observing(path):
        observed.append(str(path)); return observe(path)
    def removing(path):
        removed.append(str(path)); return remove(path)
    async def advance_allowing_use():
        await runtime.flow(context)
        await runtime.settle()
    try:
        baseline = await state(database)
        with patch.object(work_files, '_observe_work_file', observing), patch.object(work_files, '_remove_work_file', removing):
            await advance_allowing_use()
            kept = await state(database)
            assert runtime.processed == {i:WorkFileSingleOutcome.KEPT for i in (1,2,3)}
            assert runtime.history.scan.remaining == 125
            assert observed == removed == []
            release.set(); await owner
            assert not runtime.executor.unfinished_files()
            await one_round(runtime, context)
            await runtime.first_cleanup((1,2,3), database=database, occurred_at=context.clock.utc_micros())
            same_run = await state(database)
            assert same_run == kept
            assert observed == removed == []
            for _, relative in files:
                assert digest(target / 'staging' / relative) == digest(original_staging / relative)
            next_run = WorkFileRuntime(staging=target / 'staging', limits=WorkFileLimits(32, 128))
            await next_run.initialize(database)
            await one_round(next_run, context)
            deleted = await state(database)
            assert deleted['files'] == [(1,2,4),(2,2,4),(3,2,4)]
            assert len(observed) == len(removed) == 3
            assert next_run.history.scan.remaining == 125
            after = await state(database)
            await one_round(next_run, context)
            assert await state(database) == after
            assert len(observed) == len(removed) == 3
            assert (target / 'staging/recording-inputs/unregistered.mp4').read_bytes() == b'not a cleanup candidate'
            assert all((original_staging / relative).exists() for _,relative in files)
            return {'before':baseline,'kept':kept,'same_run_after_actual_use_ended':same_run,
                    'next_normal_run':deleted,'physical_stat_calls':observed,'physical_unlink_calls':removed,
                    'unregistered_file_preserved':True,'source_files_preserved':True,'limit_per_run':128,'batch_size':32}
    finally:
        release.set()
        await owner
        await runtime.settle()
        await database.close()
        assert not database.executor._thread.is_alive()


async def bounded():
    target, original_staging, files = duplicate('bounded')
    database = await BusinessDatabase.open(lambda: open_existing(target / 'state.db', DbOpenMode.EXISTING_RW, DbConfig()))
    context = SimpleNamespace(database=database, clock=SimpleNamespace(utc_micros=lambda: int(time.time()*1_000_000)))
    snapshots=[]
    try:
        for expected in (((1,2,4),(2,1,1),(3,1,1)),((1,2,4),(2,2,4),(3,1,1))):
            runtime=WorkFileRuntime(staging=target/'staging',limits=WorkFileLimits(1,1))
            await runtime.initialize(database)
            await one_round(runtime,context)
            current=await state(database)
            assert current['files'] == list(expected)
            assert runtime.history.scan.remaining == 0
            assert current['cursor'] == (1 if len(snapshots)==0 else 2)
            await one_round(runtime,context)
            assert await state(database) == current
            snapshots.append(current)
        assert (target/'staging/recording-inputs/3.mp4').exists()
        return {'configured_batch_size':1,'configured_limit_per_run':1,'normal_runs':snapshots,'third_file_waits_next_run':True}
    finally:
        await database.close()
        assert not database.executor._thread.is_alive()


async def main():
    before=digest(SOURCE)
    result={'production_repo':str(REPO),'source':str(SOURCE),'lifecycle':await lifecycle(),'bounded':await bounded()}
    assert digest(SOURCE) == before
    result['source_database_sha256']=before
    result['source_database_preserved']=True
    (EVIDENCE/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=lambda value:value.value)+'\n')
    print(json.dumps({'same_run_keep_then_next_run_delete':True,'bounded_cursor_progression':True,'source_unchanged':True}))


asyncio.run(main())
