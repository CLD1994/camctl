"""原 READ 责任限定下载遗留回收；不依赖当前相机配置。"""

import json
from pathlib import Path
from dataclasses import replace
from decimal import Decimal
import time
import errno
from types import SimpleNamespace

import pytest

from camctl.capture.recovery import RecoveryBoundary
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.host_files.models import BoundDirectories
from camctl.outputs.read_attempts import running_read_ticket
from camctl.outputs.obtain_flow import advance_obtain
from camctl.persistence.repositories.outputs import OutputsRepository
from ..support.bootstrap_cleanup_cases import download_obtain_runtime, run_download_maintenance
from camctl.persistence.repositories.operations import OperationRepository
from camctl.operations.attempts import AttemptFinish
from camctl.operations.models import CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis, ErrorValue
from camctl.operations.validation import validate_outcome
from camctl.devices.drivers.hasselblad.driver import EVIDENCE
from camctl.persistence.models import DbOutcomeKind

from ..outputs.test_grant_reuse import read_request, local_read
from ..outputs.test_read_associations import _command
from ..outputs.test_qualification import (
    _NOW, _seed_plan, _seed_action, _seed_device_file, _seed_output,
    _seed_processing, _seed_selection_and_item,
)


@pytest.fixture
def download_config(tmp_path):
    from ..support.session import session_config
    return session_config(tmp_path, devices={})


@pytest.fixture
def read_targets(download_config):
    """原票据与文件归属专项按正式存储格式准备关系，目录由正式初始化绑定。"""
    from camctl.persistence.initialization import initialize_state, InitOutcome
    from camctl.persistence.runtime import DbOpenMode, DbConfig, open_existing
    from camctl.persistence.repositories.session import SessionRepository
    from camctl.session.clock import ClockCheckInput, check_clock, register_clock_guard
    from camctl.persistence.repositories.operations import register_operation_guards
    initialized = initialize_state(download_config, Path(download_config.paths.state_db))
    assert initialized.outcome is InitOutcome.CREATED, initialized
    owned = open_existing(Path(download_config.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        register_clock_guard()
        check = check_clock(ClockCheckInput(None, 0, 0, 0), SimpleNamespace(utc_micros=lambda: _NOW))
        saved = SessionRepository().update_lower_bound(check, new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        connection = owned.connection
        connection.execute('BEGIN IMMEDIATE')
        _seed_plan(connection, 1)
        for action in (11,):
            _seed_action(connection, action, 1, action_type=2)
            _seed_device_file(connection, action + 490, action)
            _seed_output(connection, action + 690, action, action + 490)
        for action in (31,):
            _seed_action(connection, action, 1, action_type=4)
            _seed_selection_and_item(connection, dependency_id=action, selection_id=action,
                item_id=action + 70, owner_action_id=action, source_action_id=action - 20,
                output_id=action + 670)
        _seed_processing(connection, 5, 11, 501)
        connection.execute("UPDATE recording_processing SET check_decision=3, check_basis_json='{}'")
        connection.commit()
        register_operation_guards()
        yield owned
    finally:
        owned.connection.close()


def begin_read(owned, command, prepared):
    from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind, BeginDisposition
    from camctl.outputs.slots import SlotRequest
    slot = OutputsRepository().grant_read_slot(SlotRequest(prepared.copy_id, command.occurred_at), new_operation_key(), owned)
    assert slot.kind is DbOutcomeKind.COMPLETED, slot.error
    saved = OperationRepository().begin_attempt(AttemptIntent('read', command.action_id,
        OperationKind.READ_FILE, AttemptTarget(copy_id=prepared.copy_id), None,
        AttemptConfig(5, Decimal('23.000000000000000001'), Decimal('2.1')), command.occurred_at + 1,
        copy_round=1), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert saved.value.disposition is BeginDisposition.GRANTED
    return running_read_ticket(owned, prepared.copy_id)


def make_work(staging, ticket, content=b'partial'):
    work = staging / 'read-downloads' / f'{ticket.run_id}-{ticket.attempt_id}'
    work.mkdir(parents=True)
    (work / 'partial').write_bytes(content)
    return work


def assert_maintenance_failure(observed, config, original):
    """整轮默认会话保留文件错误链，由命令诊断所有者恰好交付一次。"""
    outcome = observed['outcome']
    assert outcome.reason == 'state_db_error'
    assert observed['errors'] == [original]
    diagnostic = outcome.details['exception']
    assert diagnostic['type'] == 'camctl.session.service.StateDbFailure'
    cause = diagnostic['cause']
    assert cause['type'] == 'builtins.PermissionError'
    assert cause['message'] == str(original)
    assert cause['errno'] == original.errno
    assert cause['filename'] == original.filename
    assert cause['traceback'] and cause['traceback'][-1]['function'] in ('remove', 'fail_replace')
    text = Path(config.paths.log_file).read_text()
    assert text.count('run 会话失败: state_db_error') == 1
    details = [line.removeprefix('诊断详情: ') for line in text.splitlines() if line.startswith('诊断详情: ')]
    assert len(details) == 1
    assert json.loads(details[0]) == outcome.details


@pytest.fixture
def original_read(read_request, tmp_path):
    owned, command = read_request
    owned.connection.execute('UPDATE actions SET driver_id=? WHERE id=11', (DRIVER_ID,))
    owned.connection.commit()
    granted = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    ticket, intent = begin_read(owned, command, granted.value)
    staging = tmp_path / 'staging'
    work = make_work(staging, ticket)
    return owned, ticket, intent, staging, work


@pytest.mark.parametrize("read_request", ["device"], indirect=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [RecoveryBoundary.HOST_LOCAL_SETTLED, RecoveryBoundary.HOST_POWER_CYCLE])
async def test_unconfigured_original_read_saves_local_recovery_evidence(original_read, boundary):
    owned, ticket, intent, staging, _work = original_read
    driver = HasselbladDriver({}, BoundDirectories(staging), monotonic_ns=lambda: 0)
    async with download_obtain_runtime(owned, staging, driver, boundary=boundary, horizon=intent) as runtime:
        await advance_obtain(runtime)
    result = owned.connection.execute("SELECT result_json FROM operation_attempts WHERE run_id=?", (ticket.run_id,)).fetchone()[0]
    assert json.loads(result)["settlement"]["evidence"]["type"] == "local_read_recovery"


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
@pytest.mark.parametrize("boundary,horizon_delta,removed", [
    (RecoveryBoundary.HOST_LOCAL_SETTLED, 0, True),
    (RecoveryBoundary.HOST_POWER_CYCLE, 0, True),
    (RecoveryBoundary.UNCONFIRMED, 0, False),
    (RecoveryBoundary.HOST_LOCAL_SETTLED, -1, False),
])
async def test_cleanup_requires_original_ticket_and_confirmed_startup_boundary(
        original_read, download_config, monkeypatch, boundary, horizon_delta, removed):
    owned, ticket, intent, staging, work = original_read
    late = {}
    async def new_read_after_range(database):
        from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
        # 原 READ 先正式结束；新票据在默认会话已固定恢复范围之后建立。
        result = CallOutcome(effect=EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('read_returned', 1, {})))
        key = new_operation_key()
        await database.write(lambda worker: OperationRepository().finish_attempt(AttemptFinish(ticket,
            validate_outcome(ticket, result, EVIDENCE), _NOW + 10, retry_wait=True), key, worker),
            key=key, description='原 READ 实际返回')
        key = new_operation_key()
        saved = await database.write(lambda worker: OperationRepository().begin_attempt(AttemptIntent(
            'read', 31, OperationKind.READ_FILE, AttemptTarget(copy_id=int(ticket.target_id)), None,
            AttemptConfig(5, Decimal('10'), Decimal('0')), _NOW + 11, copy_round=1), key, worker),
            key=key, description='范围固定后新 READ')
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        late['ticket'] = saved.value.ticket
        late['work'] = make_work(staging, saved.value.ticket)
    observed = await run_download_maintenance(download_config, monkeypatch, boundary=boundary,
        after_range=new_read_after_range if horizon_delta else None)
    assert observed['outcome'] is None
    if horizon_delta:
        assert not work.exists(), '已实际返回的原 READ 遗留可回收'
        assert late['work'].exists(), '启动范围固定后的 READ 仍须保留'
        ticket = late['ticket']
    else:
        assert work.exists() is not removed
    assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket


@pytest.mark.parametrize("read_request", ["device"], indirect=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary,delta", [(RecoveryBoundary.UNCONFIRMED, 0),
    (RecoveryBoundary.HOST_LOCAL_SETTLED, -1), (RecoveryBoundary.HOST_POWER_CYCLE, -1)])
async def test_unconfigured_read_cannot_save_recovery_without_original_boundary(original_read, boundary, delta):
    owned, ticket, intent, staging, _work = original_read
    driver = HasselbladDriver({}, BoundDirectories(staging), monotonic_ns=lambda: 0)
    async with download_obtain_runtime(owned, staging, driver, boundary=boundary, horizon=intent + delta) as runtime:
        await advance_obtain(runtime)
    assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_completed_read_is_cleaned_without_camera_or_recovery_assumption(
        original_read, download_config, monkeypatch):
    owned, ticket, intent, staging, work = original_read
    result = CallOutcome(effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('read_returned', 1, {})))
    receipt = OperationRepository().finish_attempt(AttemptFinish(ticket,
        validate_outcome(ticket, result, EVIDENCE), _NOW + 20), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    unknown = staging / 'read-downloads' / '999-1'
    unknown.mkdir()
    (unknown / 'keep').write_bytes(b'unknown')
    observed = await run_download_maintenance(download_config, monkeypatch, boundary=RecoveryBoundary.UNCONFIRMED)
    assert observed['outcome'] is None
    assert not work.exists()
    assert (unknown / 'keep').read_bytes() == b'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_current_actual_download_owner_prevents_recovery_deletion(
        original_read, download_config, monkeypatch):
    from camctl.host_files.downloads import DownloadWorkspace
    owned, ticket, intent, staging, work = original_read
    owner = DownloadWorkspace(staging, ticket)
    assert owner.claim()
    try:
        observed = await run_download_maintenance(download_config, monkeypatch,
            boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
        assert observed['outcome'] is None
        assert (work / 'partial').read_bytes() == b'partial'
        assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket
    finally:
        owner.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_default_session_cleans_original_download_before_business_resume(
        original_read, download_config, monkeypatch):
    owned, ticket, intent, staging, work = original_read
    observed = await run_download_maintenance(download_config, monkeypatch,
        boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
    assert observed['outcome'] is None and observed['stopped_after_batch']
    assert not work.exists()
    assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_unknown_prefix_never_hides_known_responsibility_across_runs(
        original_read, download_config, monkeypatch):
    owned, ticket, intent, staging, work = original_read
    unknown = work.parent / 'unknown-keep'
    unknown.mkdir()
    (unknown / 'keep').write_bytes(b'private unknown bytes')
    for _ in range(3):
        observed = await run_download_maintenance(download_config, monkeypatch,
            boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, limit=1)
        assert observed['outcome'] is None
        if not work.exists():
            break
    assert not work.exists(), '未知目录永久保留时，已知责任仍必须可达'
    assert (unknown / 'keep').read_bytes() == b'private unknown bytes'


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_last_budget_original_ticket_waits_for_maintenance_then_resumes(
        read_request, download_config, tmp_path, monkeypatch):
    from camctl.devices.read_session import ReadPreparationError, ReadPreparationSettlement
    from camctl.operations.models import AttemptStatus
    from ..devices.test_hasselblad_download import DownloadTransport, source, RAW
    owned, command = read_request
    fixed = source()
    owned.connection.execute('UPDATE device_files SET identity_key=?,locator_json=?,size_bytes=? WHERE id=501',
        (fixed.file_id, json.dumps(dict(fixed.locator)), len(RAW)))
    owned.connection.execute('UPDATE actions SET driver_id=? WHERE id=11', (DRIVER_ID,))
    owned.connection.commit()
    granted = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    prepared = granted.value
    first, _ = begin_read(owned, command, prepared)
    failed = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
        error=ErrorValue('device_error', 'read'),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('read_returned', 1, {})))
    saved = OperationRepository().finish_attempt(AttemptFinish(first,
        validate_outcome(first, failed, EVIDENCE), _NOW + 2, retry_wait=True), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    # 重试等待由单调时钟推进；不以缩短原票据的间隔跳过等待。
    from camctl.operations.attempts import RetryWaitGate
    gate = RetryWaitGate()
    gate.established('read', 0)
    assert gate.pending('read', interval_s=Decimal('2.1'), now_ns=0) == Decimal('2.1')
    assert gate.pending('read', interval_s=Decimal('2.1'), now_ns=2_100_000_000) is None
    ticket, intent = begin_read(owned, command, prepared)
    assert ticket.attempt_id == 2
    budgets = owned.connection.execute(
        "SELECT max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_attempts WHERE run_id=? ORDER BY attempt_no",
        (ticket.run_id,)).fetchall()
    assert [(maximum, Decimal(timeout), Decimal(retry)) for maximum, timeout, retry in budgets] == [
        (5, Decimal('23.000000000000000001'), Decimal('2.1'))] * 2
    staging = tmp_path / 'staging'
    earlier = make_work(staging, first, b'earlier settled residue')
    work = make_work(staging, ticket, b'old incomplete download')
    transport = DownloadTransport()
    driver = HasselbladDriver({'camera-1': {'kind': 'camera', 'driver': DRIVER_ID,
        'hasselblad': {'executable': '/tool/hb_cli', 'serial': 'SERIAL', 'product_id': 10}}},
        BoundDirectories(staging), monotonic_ns=time.monotonic_ns, transport=transport)
    observed = await run_download_maintenance(download_config, monkeypatch,
        boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, limit=1)
    assert observed['outcome'] is None
    assert not earlier.exists() and work.exists(), '本轮额度只够更早的责任'
    async with download_obtain_runtime(owned, staging, driver,
            boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, horizon=intent,
            configured=True, max_attempts=2) as runtime:
        with pytest.raises(ReadPreparationError) as waiting:
            await advance_obtain(runtime)
        assert waiting.value.settlement is ReadPreparationSettlement.WAITING
        assert running_read_ticket(owned, prepared.copy_id)[0] == ticket
        assert transport.calls == [], '本地维护前提未满足时不能开始相机下载'
        assert owned.connection.execute('SELECT attempts_used,status FROM operation_runs WHERE id=?',
            (ticket.run_id,)).fetchone() == (2, 2)
        observed = await run_download_maintenance(download_config, monkeypatch,
            boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, limit=1)
        assert observed['outcome'] is None and not work.exists()
        await advance_obtain(runtime)
        # 真实读取采用原最后一次票据并保存原完整内容；没有消耗新次数。
        assert [call.argv[3] for call in transport.calls] == ['list', 'info', 'download']
        current = owned.connection.execute(
            'SELECT max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_attempts WHERE run_id=? AND attempt_no=?',
            (ticket.run_id, ticket.attempt_id)).fetchone()
        assert (current[0], Decimal(current[1]), Decimal(current[2])) == (2, Decimal('10'), Decimal('0'))
        assert owned.connection.execute('SELECT attempts_used FROM operation_runs WHERE id=?',
            (ticket.run_id,)).fetchone() == (2,)
        delivered, = owned.connection.execute('SELECT f.relative_path FROM file_copies c JOIN intermediate_files f ON f.id=c.target_file_id WHERE c.id=?',
            (prepared.copy_id,)).fetchone()
        assert (staging / delivered).read_bytes() == RAW
        status, raw, error = owned.connection.execute(
            'SELECT status,result_json,error_json FROM operation_attempts WHERE run_id=? AND attempt_no=?',
            (ticket.run_id, ticket.attempt_id)).fetchone()
        assert status == 2 and error is None
        assert json.loads(raw)['settlement']['basis'] == 'observed'


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
@pytest.mark.parametrize('blocked', ['active', 'unconfirmed', 'late', 'delete_failure'])
async def test_bounded_cursor_advances_past_retained_or_failed_known_responsibility(
        read_request, download_config, tmp_path, monkeypatch, blocked):
    from camctl.host_files import downloads
    from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
    owned, command = read_request
    _seed_action(owned.connection, 12, 1, action_type=2, status=3)
    _seed_device_file(owned.connection, 502, 12)
    _seed_output(owned.connection, 702, 12, 502)
    _seed_action(owned.connection, 32, 1, action_type=4)
    _seed_selection_and_item(owned.connection, dependency_id=32, selection_id=32,
        item_id=102, owner_action_id=32, source_action_id=12, output_id=702)
    owned.connection.execute('UPDATE actions SET driver_id=?,status=3 WHERE id IN (11,12)', (DRIVER_ID,))
    owned.connection.execute("UPDATE actions SET device_id='cam-2' WHERE id=12")
    owned.connection.commit()
    first_grant = OutputsRepository().grant_file(command, new_operation_key(), owned)
    second_command = replace(command, action_id=32, item_id=102, output_id=702, source_device_file_id=502)
    second_grant = OutputsRepository().grant_file(second_command, new_operation_key(), owned)
    assert first_grant.kind is DbOutcomeKind.COMPLETED, first_grant.error
    assert second_grant.kind is DbOutcomeKind.COMPLETED, second_grant.error
    from camctl.outputs.slots import SlotRequest
    for granted in (first_grant, second_grant):
        slot = OutputsRepository().grant_read_slot(SlotRequest(granted.value.copy_id, _NOW), new_operation_key(), owned)
        assert slot.kind is DbOutcomeKind.COMPLETED, slot.error
    staging = tmp_path / 'staging'
    pair = {}

    def intent(command, prepared):
        return AttemptIntent('read', command.action_id, OperationKind.READ_FILE,
            AttemptTarget(copy_id=prepared.copy_id), None,
            AttemptConfig(5, Decimal('10'), Decimal('0')), _NOW + 10, copy_round=1)

    def record(first, second):
        pair.update(first=first, second=second,
            first_work=make_work(staging, first),
            second_work=make_work(staging, second, b'completed residue'))

    def finish(second):
        outcome = CallOutcome(effect=EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('read_returned', 1, {})))
        return AttemptFinish(second, validate_outcome(second, outcome, EVIDENCE), _NOW + 20)

    async def create_after_range(database):
        tickets = []
        for cmd, granted in ((command, first_grant), (second_command, second_grant)):
            key = new_operation_key()
            saved = await database.write(lambda worker: OperationRepository().begin_attempt(
                intent(cmd, granted.value), key, worker), key=key, description='范围固定后新 READ')
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
            tickets.append(saved.value.ticket)
        key = new_operation_key()
        saved = await database.write(lambda worker: OperationRepository().finish_attempt(
            finish(tickets[1]), key, worker), key=key, description='第二 READ 实际返回')
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        record(*tickets)

    if blocked != 'late':
        first, _ = begin_read(owned, command, first_grant.value)
        second, _ = begin_read(owned, second_command, second_grant.value)
        saved = OperationRepository().finish_attempt(finish(second), new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        record(first, second)
    owner = None
    if blocked == 'active':
        owner = downloads.DownloadWorkspace(staging, pair['first'])
        assert owner.claim()
    real_remove = downloads.shutil.rmtree
    failure = PermissionError(errno.EACCES, 'original directory delete denied', str(pair.get('first_work')))
    failures = []
    def remove(path):
        if path == pair.get('first_work') and blocked == 'delete_failure':
            failures.append(failure)
            raise failure
        return real_remove(path)
    monkeypatch.setattr(downloads.shutil, 'rmtree', remove)
    boundary = RecoveryBoundary.UNCONFIRMED if blocked == 'unconfirmed' else RecoveryBoundary.HOST_LOCAL_SETTLED
    try:
        for run in range(2):
            observed = await run_download_maintenance(download_config, monkeypatch,
                boundary=boundary, limit=1, after_range=create_after_range if blocked == 'late' and run == 0 else None)
            if blocked == 'delete_failure' and run == 0:
                assert_maintenance_failure(observed, download_config, failure)
                assert failures == [failure]
            else:
                assert observed['outcome'] is None
            assert len(observed['cursor_writes']) == 1
            if run == 0:
                assert pair['first_work'].exists() and pair['second_work'].exists()
        assert pair['first_work'].exists() and not pair['second_work'].exists()
        assert (pair['first_work'] / 'partial').read_bytes() == b'partial'
        if owner is not None:
            owner.release()
        monkeypatch.setattr(downloads.shutil, 'rmtree', real_remove)
        observed = await run_download_maintenance(download_config, monkeypatch,
            boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, limit=1)
        assert observed['outcome'] is None
        assert not pair['first_work'].exists(), '游标完成一圈后必须重新核实曾保留的责任'
    finally:
        if owner is not None:
            owner.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_cursor_write_failure_is_a_real_maintenance_error(
        original_read, download_config, monkeypatch):
    from camctl.host_files import downloads
    owned, ticket, intent, staging, work = original_read
    failure = PermissionError(errno.EACCES, 'cursor replace denied', str(staging / '.read-download-cleanup-cursor'))
    failures = []
    def fail_replace(*_args):
        failures.append(failure)
        raise failure
    monkeypatch.setattr(downloads.os, 'replace', fail_replace)
    observed = await run_download_maintenance(download_config, monkeypatch,
        boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, limit=1)
    assert_maintenance_failure(observed, download_config, failure)
    assert failures == [failure]
    assert not work.exists()
    assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_known_actual_owner_blocks_both_resume_and_unavailable_recovery(original_read):
    from camctl.host_files.downloads import DownloadWorkspace
    owned, ticket, intent, staging, work = original_read
    owner = DownloadWorkspace(staging, ticket)
    assert owner.claim()
    driver = HasselbladDriver({}, BoundDirectories(staging), monotonic_ns=lambda: 0)
    try:
        for configured in (True, False):
            async with download_obtain_runtime(owned, staging, driver,
                    boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, horizon=intent, configured=configured) as runtime:
                await advance_obtain(runtime)
            assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket
            assert (work / "partial").read_bytes() == b"partial"
            assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (ticket.attempt_id,)
    finally:
        owner.release()
