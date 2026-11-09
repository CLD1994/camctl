"""普通交付读取的真实来源、正式装配与独立进程故障边界。"""

import asyncio
import hashlib
import sqlite3
import sys
import threading
from io import BytesIO
from pathlib import Path

from camctl.capture.handlers import capture_handler
from camctl.capture.result_inputs import ObservedFile, RESULT_PAGE_CONTRACT
from camctl.capture.results import FileKind
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.devices.read_session import ReadSession
from camctl.host_files.tasks import FileTaskExecutor
from camctl.outputs.obtain_flow import advance_obtain
from camctl.persistence.repositories.file_batches import register_file_batch_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.session.supervision import Supervisor

from ..bootstrap.test_media_assembly import _config
from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, ResultCatalog, consumer_world
from ..capture.test_capture_contract import ResultsDouble
from .database import business_database
from .outputs import obtain_factory, output_drivers, prepare_obtain_selection

CONTENT = b'0123456789'
EVIDENCE = EvidenceRegistry((
    EvidenceContract('read_returned', 1, 'read', frozenset()),
    EvidenceContract('file_digest', 1, 'digest', frozenset({'file_id', 'sha256'}), identity_field='file_id'),
))


class Catalog(ResultCatalog):
    def action_types(self):
        return super().action_types() | {'obtain_action_outputs'}


class Stream(BytesIO):
    def cancel(self):
        pass


class Driver:
    def __init__(self, *, digest_error=None, digest_errors=None, on_digest=None):
        self.calls = []
        self.ends = []
        self.digest_calls = 0
        self.digest_error = digest_error
        self.digest_errors = {} if digest_errors is None else digest_errors
        self.digest_targets = []
        self.on_digest = on_digest
        self.diagnostics = []

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        self.calls.append((offset, ticket))
        owner = self

        class ObservedSession(ReadSession):
            async def wait_stopped(self):
                end = await super().wait_stopped()
                owner.ends.append(end)
                return end

        return ObservedSession(source, offset, Stream(CONTENT[offset:]), idle_timeout_s)

    async def digest(self, request):
        self.digest_calls += 1
        file_id = str(request.params['file_id'])
        self.digest_targets.append(file_id)
        if self.on_digest is not None:
            await self.on_digest()
        error = self.digest_errors.get(file_id, self.digest_error)
        if error:
            return DeviceCallResult((), {'reason': error})
        return DeviceCallResult((DeviceObservation('file_digest', 1, {
            'file_id': request.params['file_id'], 'sha256': hashlib.sha256(CONTENT).hexdigest(),
        }),), None)


async def prepare_request(tmp_path, *, now, sibling=False):
    """通过受理、实际拍摄处理和选择建立普通交付，尚未开始读取。"""
    register_file_batch_guards()
    cfg = _config(tmp_path)
    staging = Path(cfg.paths.staging)

    def capture_options(owned, _driver, _defaults):
        action, = owned.connection.execute('SELECT id FROM actions').fetchone()
        entries = tuple(ObservedFile(identity=f'delivery-{name}',
            locator={'path': f'/DCIM/{name}.jpg'},
            evidence={'listing': name}, complete=True, size_bytes=len(CONTENT),
            kind=FileKind.PHOTO, original_name=f'{name}.jpg', media_type='image/jpeg')
            for name in (('source', 'sibling') if sibling else ('source',)))
        return {'staging': staging, 'results': ResultsDouble({action: entries}),
                'evidence': RESULT_EVIDENCE.with_contract(RESULT_PAGE_CONTRACT)}

    owned, capture, source, handler = await consumer_world(
        tmp_path, 'photo', catalog=Catalog(), start=False, now_us=now,
        runtime_options=capture_options)
    try:
        await capture_handler(handler)(source, capture)
        outputs = tuple(row[0] for row in owned.connection.execute(
            'SELECT id FROM outputs WHERE source_action_id=? ORDER BY id', (source,)))
        assert len(outputs) == (2 if sibling else 1)
        action, _ = prepare_obtain_selection(owned, catalog=Catalog(), now=now, source_id=source,
                                             mode='default', request_id=2)
        for name in ('staging', 'ready', 'processing'):
            (tmp_path / name).mkdir(parents=True, exist_ok=True)
        return owned, action, outputs if sibling else outputs[0]
    except BaseException:
        owned.connection.close()
        raise


def factory(tmp_path, driver, *, now, boundary, horizon, executor):
    return obtain_factory(devices={'cam-1': {'driver': 'camctl-adb', 'copy': {
            'max_read_attempts': 3, 'read_idle_timeout_s': 10, 'retry_interval_s': 0}}},
        drivers=output_drivers(driver, EVIDENCE, read=True, digest=True, local_read_recovery=True),
        staging=tmp_path / 'staging', ready=tmp_path / 'ready', processing=tmp_path / 'processing',
        occurred_at=lambda: now + 1, monotonic_ns=lambda: 0, segment_size=len(CONTENT),
        recovery_boundary=boundary, recovery_max_event_id=None if horizon is None else lambda: horizon,
        on_recovery_diagnostic=driver.diagnostics.append, file_executor=executor)


async def interrupted_process(path, root, marker, now):
    """在 READ_END 更新的真实 SQLite 授权点等待父进程结束本进程。"""
    register_file_batch_guards()
    register_outputs_guards()
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.persistence.repositories.operations import register_operation_guards
    register_operation_guards()
    async with business_database(path) as database:
        def authorize(operation, table, column, _database, _trigger):
            if operation == sqlite3.SQLITE_UPDATE and table == 'file_copies' and column == 'read_end_event_id':
                marker.write_text('read end update entered')
                if not threading.Event().wait(30):
                    raise TimeoutError('等待父进程结束读取拥有者超时')
            return sqlite3.SQLITE_OK
        await database.read(lambda owned: owned.connection.set_authorizer(authorize), description='设置存储门闩')
        supervisor = Supervisor()
        async def record_digest():
            marker.with_suffix('.digest').write_text('digest entered')
        runtime = factory(root, Driver(on_digest=record_digest), now=now, boundary=RecoveryBoundary.UNCONFIRMED,
                          horizon=None, executor=FileTaskExecutor(supervisor))(database)
        await advance_obtain(runtime)
        raise AssertionError('未命中 READ_END 存储门闩')


if __name__ == '__main__':
    asyncio.run(interrupted_process(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])))
