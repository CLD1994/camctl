"""源摘要查询失败的原 READ 结束与交付收场。"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
import pytest_asyncio

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.capture.recovery import RecoveryBoundary
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.devices.read_session import ReadSession
from camctl.outputs.obtain_flow import advance_obtain
from camctl.persistence.repositories.acceptance import register_acceptance_guards
from camctl.persistence.repositories.cancellation import register_cancellation_guards
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import OperationRepository, register_operation_guards
from camctl.persistence.repositories.file_batches import register_file_batch_guards
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.models import DbOutcome, DbOutcomeKind

from .test_qualification import _NOW
from ..support.capture import capture_database
from ..support.digest_read_recovery_cases import Driver, factory, prepare_request
from .test_cleanup_query_commit_recovery import _CommitFaultConnection

register_acceptance_guards()
register_cancellation_guards()
register_capture_guards()
register_operation_guards()
register_file_batch_guards()

pytestmark = pytest.mark.asyncio

_CONTENT = b"0123456789"


@pytest_asyncio.fixture
async def public_read_case(tmp_path):
    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.session.supervision import Supervisor

    owned, action, output = await prepare_request(tmp_path, now=_NOW)
    driver = Driver(digest_error="source SHA query failed")
    supervisor = Supervisor()
    runtime = factory(tmp_path, driver, now=_NOW, boundary=RecoveryBoundary.UNCONFIRMED,
                      horizon=None, executor=FileTaskExecutor(supervisor))(
                          await capture_database(owned))
    try:
        yield owned, action, output, driver, runtime
    finally:
        try:
            assert not (await supervisor.drain_required()).failed
        finally:
            owned.connection.close()


def _delivery_read_state(owned, output):
    return owned.connection.execute(
        "SELECT r.status,t.status,d.status FROM operation_runs r"
        " JOIN operation_attempts t ON t.run_id=r.id"
        " JOIN file_copies c ON c.id=r.copy_id"
        " JOIN deliveries d ON d.id=c.delivery_id WHERE d.output_id=?", (output,)
    ).fetchone()


class _ReadStream:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.position = 0

    def read(self, limit: int) -> bytes:
        data = self.content[self.position:self.position + limit]
        self.position += len(data)
        return data

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ReadDriver:
    def __init__(self) -> None:
        self.offsets: list[int] = []
        self.ends = []
        self.recovery_diagnostics = []
        self.digests = {}

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        self.offsets.append(offset)
        session = ReadSession(source, offset, _ReadStream(_CONTENT[offset:]), idle_timeout_s)
        wait_stopped = session.wait_stopped
        async def wait():
            end = await wait_stopped()
            self.ends.append(end)
            return end
        session.wait_stopped = wait
        return session

    async def control(self, request):
        raise AssertionError("内部读取不发出新拍摄")

    async def digest(self, request):
        result = await self.digests[int(request.params["file_id"])].read_digest()
        if result.error is not None:
            return DeviceCallResult((), {"reason": result.error})
        return DeviceCallResult((DeviceObservation("file_digest", 1, {
            "file_id": request.params["file_id"], "sha256": result.digest,
        }),), None)



async def test_source_digest_failure_finishes_original_read_and_preserves_sibling_success(
        tmp_path, capture_resources):
    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.session.supervision import Supervisor

    owned, action, outputs = await prepare_request(tmp_path, now=_NOW, sibling=True)
    supervisor = Supervisor()
    try:
        sources = owned.connection.execute(
            "SELECT o.id,o.device_file_id,a.device_id FROM outputs o"
            " JOIN actions a ON a.id=o.source_action_id WHERE o.id IN (?,?) ORDER BY o.id",
            outputs,
        ).fetchall()
        assert len(sources) == 2 and all(row[2] == "cam-1" for row in sources)
        assert sources[0][1] != sources[1][1]
        failed_output, failed_file, _ = sources[0]
        good_output, good_file, _ = sources[1]
        driver = Driver(digest_errors={str(failed_file): "source SHA query failed"})
        runtime = factory(tmp_path, driver, now=_NOW, boundary=RecoveryBoundary.UNCONFIRMED,
                          horizon=None, executor=FileTaskExecutor(supervisor))(
                              await capture_database(owned))
        await advance_obtain(runtime)

        states = owned.connection.execute(
            "SELECT c.source_device_file_id,c.committed_bytes,c.verification_state,"
            " c.source_sha256,c.target_sha256,c.verification_error_json,"
            " c.recopies_used,d.status,d.error_json,i.source_dependency,c.slot_device_id,"
            " r.status,r.error_json,r.attempts_used,r.retry_wait_required,t.status,t.result_json"
            " FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id"
            " JOIN obtain_items i ON i.delivery_id=d.id"
            " JOIN operation_runs r ON r.copy_id=c.id"
            " JOIN operation_attempts t ON t.run_id=r.id"
            " WHERE d.action_id=? ORDER BY c.source_device_file_id", (action,),
        ).fetchall()
        assert len(states) == 2
        failed, good = states
        verification = enum_for("file_copies.verification_state")
        delivery = enum_for("deliveries.status")
        run = enum_for("operation_runs.status")
        attempt = enum_for("operation_attempts.status")
        digest = hashlib.sha256(_CONTENT).hexdigest()

        assert failed[:5] == (failed_file, 10, int(verification.FAILED), None, digest)
        assert parse_exact_json(failed[5]) == {"reason": str({"reason": "source SHA query failed"})}
        assert failed[6] == 0
        assert failed[7] == int(delivery.FAILED)
        expected_error = {
            "code": "source_file_unconfirmed", "stage": "source_read",
            "details": {"output_id": str(failed_output)},
        }
        assert parse_exact_json(failed[8]) == expected_error
        assert failed[9:11] == (0, None)
        assert failed[11] == int(run.FAILED)
        assert parse_exact_json(failed[12]) == expected_error
        assert failed[13:15] == (1, 0)
        assert failed[15] == int(attempt.SUCCEEDED)
        assert failed[16] is not None
        assert good[0:3] == (good_file, 10, int(verification.MATCHED))
        assert good[3:5] == (digest, digest)
        assert good[7] in (int(delivery.PREPARING), int(delivery.PREPARED))
        assert good[11] == int(run.SUCCEEDED)
        assert good[15] == int(attempt.SUCCEEDED)
        assert sorted(driver.digest_targets) == sorted((str(failed_file), str(good_file)))
        assert len(driver.calls) == 2 and all(offset == 0 for offset, _ in driver.calls)
        assert all(end.stopped and end.error is None for end in driver.ends)

        await advance_obtain(runtime)
        published, filename = owned.connection.execute(
            "SELECT status,file_name FROM deliveries WHERE action_id=? AND output_id=?",
            (action, good_output),
        ).fetchone()
        assert published == int(delivery.PUBLISHED)
        assert (tmp_path / "ready" / filename).read_bytes() == _CONTENT
        assert driver.digest_calls == 2
    finally:
        try:
            assert not (await supervisor.drain_required()).failed
        finally:
            owned.connection.close()


async def test_unknown_attempt_finish_reuses_original_key_without_requery(
        public_read_case, monkeypatch):
    owned, _action, output, driver, runtime = public_read_case
    finish = OperationRepository.finish_attempt
    calls, faults = [], []

    def unknown_after_commit(repository, request, key, current):
        if request.run_finish is not None and request.run_finish.error is not None \
                and request.run_finish.error.code == "source_file_unconfirmed":
            calls.append((request, key))
            if len(calls) == 1:
                fault = _CommitFaultConnection(current.connection, after_commit=True)
                faults.append(fault)
                current = replace(current, connection=fault)
        return finish(repository, request, key, current)

    monkeypatch.setattr(OperationRepository, "finish_attempt", unknown_after_commit)
    with pytest.raises(ConsistencyError, match="原读取结果未可靠保存"):
        await advance_obtain(runtime)
    assert len(calls) == 1 and faults[0].actual_commits == 1
    assert faults[0].transaction_at_failure is False
    await advance_obtain(runtime)

    assert len(calls) == 2 and calls[0] == calls[1]
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (str(calls[0][1]),),
    ).fetchone()[0] == 1
    assert len(driver.calls) == driver.digest_calls == 1
    assert driver.ends and driver.ends[-1].stopped and driver.ends[-1].error is None
    assert _delivery_read_state(owned, output) == (
        int(enum_for("operation_runs.status").FAILED),
        int(enum_for("operation_attempts.status").SUCCEEDED),
        int(enum_for("deliveries.status").FAILED),
    )


async def test_unknown_delivery_failure_reuses_original_key_without_duplicate_event(
        public_read_case, monkeypatch):
    owned, _action, output, driver, runtime = public_read_case
    fail_delivery = OutputsRepository.fail_read_delivery
    calls, faults = [], []

    def unknown_after_commit(repository, request, key, current):
        calls.append((request, key))
        if len(calls) == 1:
            fault = _CommitFaultConnection(current.connection, after_commit=True)
            faults.append(fault)
            current = replace(current, connection=fault)
        return fail_delivery(repository, request, key, current)

    monkeypatch.setattr(OutputsRepository, "fail_read_delivery", unknown_after_commit)
    with pytest.raises(ConsistencyError, match="原读取业务结果未可靠保存"):
        await advance_obtain(runtime)
    assert len(calls) == 1 and faults[0].actual_commits == 1
    assert faults[0].transaction_at_failure is False
    await advance_obtain(runtime)

    assert len(calls) == 2 and calls[0] == calls[1]
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (str(calls[0][1]),),
    ).fetchone()[0] == 1
    assert len(driver.calls) == driver.digest_calls == 1
    assert driver.ends and driver.ends[-1].stopped and driver.ends[-1].error is None
    assert _delivery_read_state(owned, output) == (
        int(enum_for("operation_runs.status").FAILED),
        int(enum_for("operation_attempts.status").SUCCEEDED),
        int(enum_for("deliveries.status").FAILED),
    )


async def test_saved_read_end_and_digest_failure_resume_without_device_calls(tmp_path, capture_resources):
    import sqlite3
    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.session.supervision import Supervisor
    from ..support.digest_read_recovery_cases import Driver, factory, prepare_request

    owned, action, output = await prepare_request(tmp_path, now=_NOW)
    try:
        database = await capture_database(owned)
        denied = []

        async def deny_attempt_save():
            def authorize(operation, table, column, _database, _trigger):
                if operation == sqlite3.SQLITE_UPDATE and table == 'operation_attempts':
                    denied.append((table, column))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            await database.read(lambda current: current.connection.set_authorizer(authorize),
                                description='注入读取结果保存故障')

        driver = Driver(digest_error='source SHA query failed', on_digest=deny_attempt_save)
        supervisor = Supervisor()
        first = factory(tmp_path, driver, now=_NOW, boundary=RecoveryBoundary.UNCONFIRMED,
                        horizon=None, executor=FileTaskExecutor(supervisor))(database)
        try:
            with pytest.raises(ConsistencyError):
                await advance_obtain(first)
        finally:
            await database.read(lambda current: current.connection.set_authorizer(None),
                                description='恢复数据库授权器')
        assert denied and len(driver.calls) == 1 and driver.digest_calls == 1
        row = owned.connection.execute(
            'SELECT c.id,c.committed_bytes,c.read_end_event_id,c.verification_state,'
            'c.verification_error_json,r.id,r.attempts_used,a.status FROM file_copies c '
            'JOIN operation_runs r ON r.copy_id=c.id JOIN operation_attempts a ON a.run_id=r.id').fetchone()
        copy_id, committed, end_event, verification, error, run_id, attempts, status = row
        assert committed == 10 and end_event is not None
        assert verification == int(enum_for('file_copies.verification_state').FAILED)
        assert 'source SHA query failed' in error
        assert attempts == 1 and status == int(enum_for('operation_attempts.status').RUNNING)
        assert not (await supervisor.drain_required()).failed
        database = await capture_resources.restart_database(owned)
        recovered_driver = Driver()
        recovered_supervisor = Supervisor()
        recovered = factory(tmp_path, recovered_driver, now=_NOW,
            boundary=RecoveryBoundary.UNCONFIRMED, horizon=None,
            executor=FileTaskExecutor(recovered_supervisor))(database)
        await advance_obtain(recovered)
        assert recovered_driver.calls == [] and recovered_driver.digest_calls == 0
        row = owned.connection.execute(
            'SELECT r.id,r.attempts_used,r.status,a.status,d.status,c.read_end_event_id '
            'FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id '
            'JOIN operation_attempts a ON a.run_id=r.id JOIN deliveries d ON d.id=c.delivery_id '
            'WHERE c.id=?', (copy_id,)).fetchone()
        assert row == (run_id, 1, int(enum_for('operation_runs.status').FAILED),
            int(enum_for('operation_attempts.status').SUCCEEDED),
            int(enum_for('deliveries.status').FAILED), end_event)
        delivery = owned.connection.execute(
            'SELECT d.action_id,d.output_id,d.error_json,c.verification_error_json '
            'FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id WHERE c.id=?',
            (copy_id,)).fetchone()
        assert delivery[:2] == (action, output)
        assert parse_exact_json(delivery[2])['code'] == 'source_file_unconfirmed'
        assert 'source SHA query failed' in delivery[3]
        assert not (await recovered_supervisor.drain_required()).failed
    finally:
        owned.connection.close()


async def test_process_exit_before_read_end_requires_boundary_and_confirms_eof(tmp_path, capture_resources):
    import asyncio
    import os
    import sys
    from pathlib import Path
    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.outputs.read_attempts import running_read_ticket
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from camctl.session.supervision import Supervisor
    from ..support.digest_read_recovery_cases import CONTENT, Driver, factory, prepare_request

    owned, action, output = await prepare_request(tmp_path, now=_NOW)
    path = tmp_path / 'state.db'
    await capture_resources.close()
    owned.connection.close()
    marker = tmp_path / 'read-end-entered'
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    tests_root = Path(__file__).resolve().parents[2]
    src_root = tests_root.parent / 'src'
    env['PYTHONPATH'] = os.pathsep.join((str(tests_root), str(src_root), env.get('PYTHONPATH', '')))
    process = await asyncio.create_subprocess_exec(sys.executable, '-m',
        'integration.support.digest_read_recovery_cases', str(path), str(tmp_path), str(marker), str(_NOW),
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(20):
            while not marker.exists():
                if process.returncode is not None:
                    _, stderr = await process.communicate()
                    raise AssertionError(f'读取进程提前退出: {stderr.decode(errors="replace")}')
                await asyncio.sleep(0.01)
        assert process.returncode is None
        observer = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            copy_id, committed, end_event, relative = observer.connection.execute(
                'SELECT c.id,c.committed_bytes,c.read_end_event_id,f.relative_path FROM file_copies c '
                'JOIN intermediate_files f ON f.id=c.target_file_id').fetchone()
            assert committed == len(CONTENT) and end_event is None
            assert (tmp_path / 'staging' / relative).read_bytes() == CONTENT
            original, _intent = running_read_ticket(observer, copy_id)
        finally:
            observer.connection.close()
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode is not None
    assert not marker.with_suffix('.digest').exists()
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        database = await capture_database(owned)
        assert owned.connection.execute(
            'SELECT committed_bytes,read_end_event_id FROM file_copies WHERE id=?',
            (copy_id,)).fetchone() == (len(CONTENT), None)
        assert running_read_ticket(owned, copy_id)[0] == original
        horizon, = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()
        blocked_driver = Driver()
        supervisor = Supervisor()
        blocked = factory(tmp_path, blocked_driver, now=_NOW,
            boundary=RecoveryBoundary.UNCONFIRMED, horizon=None,
            executor=FileTaskExecutor(supervisor))(database)
        await advance_obtain(blocked)
        assert blocked_driver.calls == [] and blocked_driver.digest_calls == 0
        assert blocked_driver.diagnostics and blocked_driver.diagnostics[-1].reason.value == 'unconfirmed_boundary'
        assert running_read_ticket(owned, copy_id)[0] == original
        assert not (await supervisor.drain_required()).failed
        await capture_resources.restart_database(owned)
        database = await capture_database(owned)
        driver = Driver()
        supervisor = Supervisor()
        recovered = factory(tmp_path, driver, now=_NOW,
            boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, horizon=horizon,
            executor=FileTaskExecutor(supervisor))(database)
        await advance_obtain(recovered)
        assert driver.calls == [(len(CONTENT), original)]
        assert driver.ends and driver.ends[-1].stopped and driver.ends[-1].bytes_read == 0
        assert driver.ends[-1].error is None and driver.digest_calls == 1
        row = owned.connection.execute(
            'SELECT r.attempts_used,a.status,c.read_end_event_id,c.committed_bytes '
            'FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id '
            'JOIN operation_attempts a ON a.run_id=r.id WHERE c.id=?', (copy_id,)).fetchone()
        assert row[0] == 1 and row[1] == int(enum_for('operation_attempts.status').SUCCEEDED)
        assert row[2] is not None and row[3] == len(CONTENT)
        body, = owned.connection.execute('SELECT body_json FROM history_events WHERE id=?', (row[2],)).fetchone()
        saved = parse_exact_json(body)['evidence']['read_end']
        assert saved['ticket']['run_id'] == original.run_id
        assert saved['ticket']['attempt_no'] == original.attempt_id
        assert saved['end'] == {'stopped': True, 'bytes_read': 0, 'error': None}
        delivery = owned.connection.execute(
            'SELECT d.action_id,d.output_id,d.status,c.source_sha256,c.target_sha256,i.source_dependency '
            'FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id '
            'JOIN obtain_items i ON i.delivery_id=d.id WHERE c.id=?', (copy_id,)).fetchone()
        assert delivery[:2] == (action, output)
        assert delivery[2] in (int(enum_for('deliveries.status').PREPARED),
                               int(enum_for('deliveries.status').PUBLISHED))
        assert delivery[3:5] == (hashlib.sha256(CONTENT).hexdigest(),) * 2
        assert delivery[5] == 0
        assert (tmp_path / 'staging' / relative).read_bytes() == CONTENT
        assert not (await supervisor.drain_required()).failed
    finally:
        owned.connection.close()


async def test_held_clean_read_end_retries_saved_verification_failure_without_digest_requery(
        public_read_case, monkeypatch):
    owned, _action, output, driver, runtime = public_read_case
    finish = OperationRepository.finish_attempt
    calls = []
    enabled = True

    def deny_first_save(repository, request, key, current):
        if request.ticket.operation == "read":
            calls.append((request, key))
            if enabled:
                return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=RuntimeError("READ 保存中断"))
        return finish(repository, request, key, current)

    monkeypatch.setattr(OperationRepository, "finish_attempt", deny_first_save)
    with pytest.raises(ConsistencyError):
        await advance_obtain(runtime)
    assert len(calls) == 1
    assert driver.ends and driver.ends[-1].stopped and driver.ends[-1].error is None
    assert owned.connection.execute(
        "SELECT a.status FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " JOIN file_copies c ON c.id=r.copy_id JOIN deliveries d ON d.id=c.delivery_id"
        " WHERE d.output_id=?", (output,),
    ).fetchone()[0] == int(enum_for("operation_attempts.status").RUNNING)

    enabled = False
    await advance_obtain(runtime)

    assert len(calls) == 2 and calls[0] == calls[1]
    assert [offset for offset, _ticket in driver.calls] == [0]
    assert driver.digest_calls == 1
    assert _delivery_read_state(owned, output) == (
        int(enum_for("operation_runs.status").FAILED),
        int(enum_for("operation_attempts.status").SUCCEEDED),
        int(enum_for("deliveries.status").FAILED),
    )


async def test_reliable_cancellation_wins_after_saved_source_digest_failure(
        public_read_case, monkeypatch):
    owned, action, output, driver, runtime = public_read_case
    from ..bootstrap.test_output_binding_changes import Catalog
    from ..support.outputs import prepare_cancel_target
    save_verification = OutputsRepository.save_verification
    applied = False

    def cancel_after_verification(repository, request, key, current):
        nonlocal applied
        receipt = save_verification(repository, request, key, current)
        if receipt.kind is DbOutcomeKind.COMPLETED and not applied:
            applied = True
            prepare_cancel_target(current, catalog=Catalog(), now=_NOW + 23_000_000,
                                  action_id=action)
        return receipt

    monkeypatch.setattr(OutputsRepository, "save_verification", cancel_after_verification)
    await advance_obtain(runtime)

    row = owned.connection.execute(
        "SELECT r.status,r.error_json,t.status,d.status,d.error_json,"
        " i.source_dependency,c.slot_device_id"
        " FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
        " JOIN file_copies c ON c.id=r.copy_id"
        " JOIN deliveries d ON d.id=c.delivery_id"
        " JOIN obtain_items i ON i.delivery_id=d.id"
        " WHERE d.output_id=?", (output,)).fetchone()
    assert applied
    assert row[0] == int(enum_for("operation_runs.status").CANCELED)
    assert row[1] is None
    assert row[2] == int(enum_for("operation_attempts.status").SUCCEEDED)
    assert row[3] != int(enum_for("deliveries.status").FAILED)
    assert row[4] is None
    assert row[5] == 1
    assert row[6] is not None
    assert len(driver.calls) == driver.digest_calls == 1
    assert driver.ends and driver.ends[-1].stopped and driver.ends[-1].error is None
