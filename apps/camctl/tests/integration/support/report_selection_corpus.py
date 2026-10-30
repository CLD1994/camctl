"""报告历史大窗口的真实来源生成与固定样本读取。"""

def submit_many(owned, count, request_id):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode, PlanDisposition, ProcessInput
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from ..history.test_report_scope import _Catalog, _plan_body, _past_schedule
    body = _plan_body(str(request_id), _past_schedule())
    prototype = body['actions'][0]
    body['actions'] = [dict(prototype, name=f'shot-{index}') for index in range(count)]
    result = AcceptanceRepository().process_input(
        ProcessInput(ParsedInput('many.json', body), _Catalog(), CommandMode.RUN, 1),
        new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.plan_disposition is PlanDisposition.REGISTERED, result.value



async def finish_many(owned, count, *, action_id=1):
    import time
    from camctl.capture.handlers import capture_handler
    from camctl.capture.results import FileKind
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
    from ..history.test_report_scope import _ActivityDriver, _PAGE_EVIDENCE
    from ..support.capture import capture_runtime
    from camctl.capture.timelapse import CaptureWaitConfig
    from ..capture.test_capture_contract import ResultsDouble, _entry
    now = int(time.time() * 1_000_000)
    started = SchedulingRepository().start_action(StartActionRequest(action_id, now, now), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    from camctl.persistence.repositories.scheduling import StartOutcome
    assert started.value.outcome is StartOutcome.STARTED, started.value
    activity_id = owned.connection.execute('SELECT id FROM device_activities WHERE action_id=?', (action_id,)).fetchone()[0]
    runtime = await capture_runtime(
        owned, driver=_ActivityDriver(activity_target=str(activity_id)), evidence=_PAGE_EVIDENCE,
        results=ResultsDouble({activity_id: tuple(_entry(f'photo-{action_id}-{index}', kind=FileKind.PHOTO) for index in range(count))}),
        wall_us=lambda: int(time.time() * 1_000_000), monotonic_ns=time.monotonic_ns,
        wait_config=lambda params: CaptureWaitConfig(target_duration_ms=600_000, driver_margin_ms=0))
    await capture_handler("camera_take_photo")(action_id, runtime)
    assert owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE source_action_id=?', (action_id,)).fetchone()[0] == count



async def populate_corpus(owned, path):
    """原受理、调度和拍摄路径生成完整的大窗口来源。"""
    from ..history.test_report_scope import _to_wm
    submit_many(owned, 3, 1)
    early_sparse_wm = _to_wm(owned.connection)
    await finish_many(owned, 4)
    output_window_end = _to_wm(owned.connection)
    submit_many(owned, 4200, 2)
    early = _to_wm(owned.connection)
    submit_many(owned, 3, 3)
    return early_sparse_wm, output_window_end, early


def _sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _facts(connection):
    """读取样本可核对的格式、历史边界和业务规模。"""
    metadata = connection.execute(
        'SELECT instance_id,format_version,staging_path,ready_path,processing_path FROM database_metadata').fetchone()
    counts = {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
              for table in ('plans', 'actions', 'outputs', 'history_transactions', 'history_events')}
    distribution = [count for _, count in connection.execute(
        'SELECT plan_id,COUNT(*) FROM actions GROUP BY plan_id ORDER BY plan_id')]
    transaction = connection.execute(
        'SELECT id,last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1').fetchone()
    return metadata, counts, distribution, transaction


def corpus_watermarks(connection):
    """以计划完整受理事务推导原三次观察的业务水位。"""
    plans = connection.execute(
        'SELECT p.id,t.id,t.first_event_id,t.last_event_id FROM plans p '
        'JOIN history_events e ON e.id=p.created_event_id '
        'JOIN history_transactions t ON t.id=e.transaction_id ORDER BY p.id').fetchall()
    assert [row[0] for row in plans] == [1, 2, 3]
    def previous(txn_id):
        row = connection.execute('SELECT last_event_id FROM history_transactions WHERE id<? ORDER BY id DESC LIMIT 1',
                                 (txn_id,)).fetchone()
        assert row is not None
        return row[0]
    def watermark(end):
        return int(connection.execute(
            'SELECT COALESCE(MAX(change_seq),0) FROM history_events WHERE id<=?', (end,)).fetchone()[0])
    output_end = previous(plans[1][1])
    outputs = connection.execute('SELECT created_event_id FROM outputs ORDER BY id').fetchall()
    assert len(outputs) == 4
    assert all(plans[0][3] < event <= output_end for (event,) in outputs)
    assert plans[0][3] < output_end < plans[1][2] <= plans[1][3] < plans[2][2]
    return watermark(plans[0][3]), watermark(output_end), watermark(previous(plans[2][1]))


def open_corpus(directory):
    """解压独立副本并只读核对样本；调用方负责退出上下文。"""
    from contextlib import contextmanager
    from pathlib import Path
    import gzip
    import json
    import shutil
    import sqlite3

    @contextmanager
    def scope():
        assets = Path(__file__).resolve().parents[1] / 'history' / 'fixtures'
        manifest = json.loads((assets / 'report-selection-4200.json').read_text())
        archive = assets / 'report-selection-4200.sqlite.gz'
        assert archive.stat().st_size == manifest['gzip_bytes']
        assert _sha256(archive) == manifest['gzip_sha256']
        state = directory / 'state.db'
        with gzip.open(archive, 'rb') as source, state.open('xb') as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
        assert state.stat().st_size == manifest['database_bytes']
        assert _sha256(state) == manifest['database_sha256']
        connection = sqlite3.connect(f'{state.resolve().as_uri()}?mode=ro', uri=True, isolation_level=None)
        try:
            connection.execute('PRAGMA query_only=ON').close()
            assert connection.execute('PRAGMA query_only').fetchone() == (1,)
            assert connection.execute('PRAGMA journal_mode').fetchone() == (manifest['journal_mode'],)
            assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            metadata, counts, distribution, boundary = _facts(connection)
            paths = manifest['metadata_paths']
            assert metadata == (manifest['database_instance_id'], manifest['database_format_version'],
                                paths['staging'], paths['ready'], paths['processing'])
            assert counts == manifest['counts']
            assert distribution == manifest['actions_per_plan']
            assert boundary == (manifest['history_boundary']['transaction_id'],
                                manifest['history_boundary']['last_event_id'])
            assert connection.execute('SELECT MAX(id) FROM history_events').fetchone() == (boundary[1],)
            watermarks = corpus_watermarks(connection)
            yield connection, state, watermarks
        finally:
            connection.close()
            assert _sha256(state) == manifest['database_sha256']
            assert _sha256(archive) == manifest['gzip_sha256']
    return scope()


def main():
    """以真实业务入口生成全新样本目录，不覆盖现有资产。"""
    import argparse
    import asyncio
    from contextlib import closing
    import gzip
    import json
    import os
    import shutil
    import sqlite3
    import tempfile
    from pathlib import Path
    from .capture import capture_test_session
    from .database import create_test_database
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

    parser = argparse.ArgumentParser(description='生成报告选择只读样本；输出目录必须不存在。')
    parser.add_argument('--output-dir', type=Path, required=True)
    output = parser.parse_args().output_dir.resolve()
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='report-corpus-', dir=output.parent) as temporary:
        workspace = Path(temporary)
        state = workspace / 'state.db'
        create_test_database(state)
        owned = open_existing(state, DbOpenMode.EXISTING_RW, DbConfig())
        async def generate():
            async with capture_test_session():
                return await populate_corpus(owned, workspace)
        try:
            expected_watermarks = asyncio.run(generate())
            assert owned.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone() == (0, 0, 0)
        finally:
            owned.connection.close()
        assert not state.with_name('state.db-wal').exists()
        assert not state.with_name('state.db-shm').exists()
        with closing(sqlite3.connect(f'{state.as_uri()}?mode=ro', uri=True)) as check:
            check.execute('PRAGMA query_only=ON')
            assert check.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            metadata, counts, distribution, boundary = _facts(check)
            assert (counts['plans'], counts['actions'], counts['outputs']) == (3, 4206, 4)
            assert distribution == [3, 4200, 3]
            assert corpus_watermarks(check) == expected_watermarks
            assert check.execute('SELECT MAX(id) FROM history_events').fetchone() == (boundary[1],)
            journal = check.execute('PRAGMA journal_mode').fetchone()[0]
        bundle = workspace / 'bundle'
        bundle.mkdir()
        archive = bundle / 'report-selection-4200.sqlite.gz'
        with state.open('rb') as source, archive.open('wb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0, compresslevel=6) as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
        manifest = {
            'description': '真实受理、调度和拍摄生成的报告历史只读样本。',
            'database_format_version': metadata[1], 'database_sha256': _sha256(state),
            'database_bytes': state.stat().st_size, 'gzip_sha256': _sha256(archive),
            'gzip_bytes': archive.stat().st_size, 'database_instance_id': metadata[0],
            'metadata_paths': dict(zip(('staging', 'ready', 'processing'), metadata[2:])),
            'journal_mode': journal, 'counts': counts, 'actions_per_plan': distribution,
            'history_boundary': {'transaction_id': boundary[0], 'last_event_id': boundary[1]},
            'origin': {'generator': 'integration.support.report_selection_corpus',
                       'note': '经正式受理、调度和拍摄入口生成；数据库本身不能证明生成路径。'},
        }
        (bundle / 'report-selection-4200.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        os.rename(bundle, output)
    print(f'已生成 {output}')


if __name__ == '__main__':
    main()
