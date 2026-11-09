"""原 READ 责任限定下载遗留回收；不依赖当前相机配置。"""

import json
from pathlib import Path
from dataclasses import replace
from decimal import Decimal
import time
import errno
from types import SimpleNamespace

import pytest
import pytest_asyncio

from camctl.capture.recovery import RecoveryBoundary
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.host_files.models import BoundDirectories
from camctl.outputs.read_attempts import running_read_ticket
from camctl.outputs.obtain_flow import advance_obtain
from camctl.persistence.repositories.outputs import OutputsRepository
from ..support.bootstrap_cleanup_cases import download_obtain_runtime, run_download_maintenance, run_default_download_session
from camctl.persistence.repositories.operations import OperationRepository
from camctl.operations.attempts import AttemptFinish
from camctl.operations.models import CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis, ErrorValue
from camctl.operations.validation import validate_outcome
from camctl.devices.drivers.hasselblad.driver import EVIDENCE
from camctl.persistence.models import DbOutcomeKind

from ..capture.test_host_photo_flow import _NOW, _input_time
from ..support.hasselblad_read_cases import prepare_device_photo



@pytest.fixture
def download_config(tmp_path):
    from ..support.session import session_config
    return session_config(tmp_path, devices={})


@pytest_asyncio.fixture
async def photo_read_request(tmp_path, capture_resources):
    """通过正式拍摄和受理入口建立相机原图及取回责任。"""
    from camctl.outputs.qualification import FileCandidate, OperationConfig
    from ..support.bootstrap_cleanup_cases import fix_obtain_selections
    from ..support.capture import capture_database
    from ..support.outputs import submit_output_document

    cfg, owned, _driver, catalog, output_id, file_id = await prepare_device_photo(
        tmp_path, capture_resources)
    try:
        database = await capture_database(owned)
        accepted = await submit_output_document(database, catalog=catalog, occurred_at=_NOW,
            body={"request_id": "2", "created_at": _input_time(), "name": "取回原图",
                "actions": [{"name": "取回", "type": "obtain_action_outputs",
                    "scheduled_at": _input_time(),
                    "params": {"source": {"action_instance_id": "1"}}}]})
        assert accepted.plan_id is not None, accepted
        action_id, = owned.connection.execute("SELECT id FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()
        fix_obtain_selections(owned, action_id, _NOW)
        item_id, = owned.connection.execute(
            "SELECT id FROM obtain_items WHERE output_id=?", (output_id,)).fetchone()
        command = FileCandidate(action_id, item_id, None, output_id, file_id,
            "part", "3fr", "原图", OperationConfig(3, Decimal("10"), Decimal("0")), _NOW)
        yield owned, command
    finally:
        owned.connection.close()


@pytest_asyncio.fixture
async def two_photo_read_requests(photo_read_request, tmp_path, capture_resources):
    """在同一状态库中经真实拍摄和受理准备第二台机身及取回。"""
    from camctl.bootstrap.flows import capture_flow
    from camctl.devices.catalog import DriverDefinitions, build_catalog
    from camctl.devices.drivers.hasselblad.definitions import driver_definition
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.operations.process import LocalExit, RawToolOutcome
    from camctl.outputs.qualification import FileCandidate, OperationConfig
    from ..capture.test_host_photo_flow import _config, _entry, _PhotoDriver, _ManagedCaptureTransport
    from ..support.bootstrap_cleanup_cases import fix_obtain_selections
    from ..support.capture import capture_database
    from ..support.outputs import submit_output_document

    owned, first = photo_read_request
    cfg = _config(tmp_path, storage='device')
    camera = dict(cfg.devices['cam-1'])
    camera['hasselblad'] = dict(camera['hasselblad'], serial='SERIAL-2', executable='/tool/camera-2')
    devices = dict(cfg.devices, **{'cam-2': camera})
    cfg = replace(cfg, devices=devices)
    catalog = build_catalog(cfg, DriverDefinitions({DRIVER_ID: driver_definition()}))

    class SecondTransport(_ManagedCaptureTransport):
        async def run(self, spec, stop):
            if spec.argv[3] == 'info':
                return RawToolOutcome(LocalExit(exit_code=0), b'serial : SERIAL-2\n', None, None, stderr=b'')
            return await super().run(spec, stop)

    driver = _PhotoDriver(Path(cfg.paths.staging))
    driver.transport = SecondTransport()
    driver.transport.fake_names = True
    driver.device_driver = HasselbladDriver(devices, BoundDirectories(Path(cfg.paths.staging)),
        monotonic_ns=lambda: 1, transport=driver.transport)
    database = await capture_database(owned)
    accepted = await submit_output_document(database, catalog=catalog, occurred_at=_NOW,
        body={'request_id': '3', 'created_at': _input_time(), 'name': '第二机身拍摄',
            'actions': [{'name': '单张 RAW', 'type': 'camera_take_photo', 'device_id': 'cam-2',
                'scheduled_at': _input_time(), 'policy': {'max_delay_ms': 5000},
                'params': {'type': 'hasselblad_photo', 'capture_format': 'raw',
                    'exposure': {'mode': 'P', 'iso': 100}, 'white_balance': {'mode': 'auto'},
                    'focus': {'mode': 'MF'}}}]})
    assert accepted.plan_id is not None, accepted
    action_id, = owned.connection.execute('SELECT id FROM actions WHERE plan_id=?', (accepted.plan_id,)).fetchone()
    factory = await capture_resources.factory(owned, driver=driver, evidence=_entry(driver).evidence,
        devices=devices, drivers=DriverRegistry((_entry(driver),)), staging=Path(cfg.paths.staging),
        wait_config=lambda _: None, wall_us=lambda: _NOW, monotonic_ns=lambda: 9_000_000_000)
    flow = capture_flow(factory)
    await flow(SimpleNamespace(database=database, clock=SimpleNamespace(utc_micros=lambda: _NOW)))
    await flow.settle()
    assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (action_id,)).fetchone() == (3,)
    output_id, file_id = owned.connection.execute(
        'SELECT o.id,o.device_file_id FROM outputs o WHERE o.source_action_id=? AND o.kind=1',
        (action_id,)).fetchone()
    accepted = await submit_output_document(database, catalog=catalog, occurred_at=_NOW,
        body={'request_id': '4', 'created_at': _input_time(), 'name': '第二机身取回',
            'actions': [{'name': '取回', 'type': 'obtain_action_outputs', 'scheduled_at': _input_time(),
                'params': {'source': {'action_instance_id': str(action_id)}}}]})
    assert accepted.plan_id is not None, accepted
    obtain, = owned.connection.execute('SELECT id FROM actions WHERE plan_id=?', (accepted.plan_id,)).fetchone()
    fix_obtain_selections(owned, obtain, _NOW)
    item, = owned.connection.execute('SELECT id FROM obtain_items WHERE output_id=?',
                                     (output_id,)).fetchone()
    second = FileCandidate(obtain, item, None, output_id, file_id, 'part', '3fr', '原图',
        OperationConfig(3, Decimal('10'), Decimal('0')), _NOW)
    return owned, first, second


def acquire_batch(owned, command):
    from camctl.outputs.batches import BatchOutcome, BatchRequest
    from camctl.persistence.repositories.file_batches import FileBatchRepository, register_file_batch_guards
    device, = owned.connection.execute(
        'SELECT a.device_id FROM device_files f JOIN actions a ON a.id=f.observer_action_id WHERE f.id=?',
        (command.source_device_file_id,)).fetchone()
    register_file_batch_guards()
    saved = FileBatchRepository().acquire(
        BatchRequest(command.action_id, device, command.occurred_at), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED and saved.value in (BatchOutcome.GRANTED, BatchOutcome.HELD), saved


def capture_selection(when, *, device='cam-1', serial='SERIAL-1'):
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    from camctl.persistence.repositories.capture_selection import CaptureSelectionInput
    return CaptureSelectionInput(device, DRIVER_ID, CameraIdentity(serial, 10),
        HasselbladDriver.default_prepare_lead_s, when)


def begin_read(owned, command, prepared, *, occurred_at=None, device='cam-1', serial='SERIAL-1'):
    from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind, BeginDisposition
    from camctl.outputs.slots import SlotRequest, SlotOutcome
    when = command.occurred_at + 1 if occurred_at is None else occurred_at
    acquire_batch(owned, command)
    slot = OutputsRepository().grant_read_slot(SlotRequest(prepared.copy_id, command.occurred_at, capture_selection(command.occurred_at, device=device, serial=serial)), new_operation_key(), owned)
    assert slot.kind is DbOutcomeKind.COMPLETED, slot.error
    assert slot.value.outcome in (SlotOutcome.GRANTED, SlotOutcome.HELD), slot.value
    saved = OperationRepository().begin_attempt(AttemptIntent('read', command.action_id,
        OperationKind.READ_FILE, AttemptTarget(copy_id=prepared.copy_id), None,
        AttemptConfig(5, Decimal('23.000000000000000001'), Decimal('2.1')), when,
        copy_round=1, capture_selection=capture_selection(when, device=device, serial=serial)), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert saved.value.disposition is BeginDisposition.GRANTED
    return running_read_ticket(owned, prepared.copy_id)


def make_work(staging, ticket, content=b'partial'):
    work = staging / 'read-downloads' / f'{ticket.run_id}-{ticket.attempt_id}'
    work.mkdir(parents=True)
    (work / 'partial').write_bytes(content)
    return work


def assert_maintenance_failure(outcome, config, original):
    """整轮默认会话保留文件错误链，由命令诊断所有者恰好交付一次。"""
    assert outcome.reason == 'state_db_error'
    diagnostic = outcome.details['exception']
    assert diagnostic['type'] == 'camctl.session.service.StateDbFailure'
    cause = diagnostic['cause']
    assert cause['type'] == 'builtins.PermissionError'
    assert cause['message'] == str(original)
    assert cause['errno'] == original.errno
    assert cause['filename'] == original.filename
    assert cause['traceback'] and cause['traceback'][-1]['function'] in ('remove', 'fail_replace')
    records = [json.loads(line) for line in Path(config.paths.log_file).read_text().splitlines()]
    failures = [record for record in records if record.get('event') == 'session_failed']
    assert len(failures) == 1
    assert (failures[0]['command'], failures[0]['reason'], failures[0]['level']) == (
        'run', 'state_db_error', 'ERROR')
    logged = failures[0]['details']['exception']
    assert logged['type'] == diagnostic['type']
    assert logged['cause'] == cause
    assert logged['context'] == {'$ref': '#/details/exception/cause'}


@pytest.fixture
def original_read(photo_read_request, tmp_path):
    owned, command = photo_read_request
    granted = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    ticket, intent = begin_read(owned, command, granted.value)
    staging = tmp_path / 'staging'
    work = make_work(staging, ticket)
    return owned, ticket, intent, staging, work


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
@pytest.mark.parametrize("boundary,horizon_delta,removed", [
    (RecoveryBoundary.HOST_LOCAL_SETTLED, 0, True),
    (RecoveryBoundary.HOST_POWER_CYCLE, 0, True),
    (RecoveryBoundary.UNCONFIRMED, 0, False),
    (RecoveryBoundary.HOST_LOCAL_SETTLED, -1, False),
])
async def test_cleanup_requires_original_ticket_and_confirmed_startup_boundary(
        original_read, photo_read_request, download_config, monkeypatch, boundary, horizon_delta, removed):
    owned, ticket, intent, staging, work = original_read
    _, command = photo_read_request
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
            'read', command.action_id, OperationKind.READ_FILE, AttemptTarget(copy_id=int(ticket.target_id)), None,
            AttemptConfig(5, Decimal('23.000000000000000001'), Decimal('2.1')), _NOW + 2_100_011, copy_round=1,
            capture_selection=capture_selection(_NOW + 2_100_011)), key, worker),
            key=key, description='范围固定后新 READ')
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        late['ticket'] = saved.value.ticket
        late['work'] = make_work(staging, saved.value.ticket)
    await run_download_maintenance(download_config, boundary=boundary,
        after_range=new_read_after_range if horizon_delta else None)
    if horizon_delta:
        assert not work.exists(), '已实际返回的原 READ 遗留可回收'
        assert late['work'].exists(), '启动范围固定后的 READ 仍须保留'
        ticket = late['ticket']
    else:
        assert work.exists() is not removed
    assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket


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
    await run_download_maintenance(download_config, boundary=RecoveryBoundary.UNCONFIRMED)
    assert not work.exists()
    assert (unknown / 'keep').read_bytes() == b'unknown'


@pytest.mark.asyncio
async def test_current_actual_download_owner_prevents_recovery_deletion(
        original_read, download_config, monkeypatch):
    from camctl.host_files.downloads import DownloadWorkspace
    owned, ticket, intent, staging, work = original_read
    owner = DownloadWorkspace(staging, ticket)
    assert owner.claim()
    try:
        await run_download_maintenance(download_config,
            boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
        assert (work / 'partial').read_bytes() == b'partial'
        assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket
    finally:
        owner.release()


@pytest.mark.asyncio
async def test_default_session_cleans_original_download_before_business_resume(
        original_read, download_config, monkeypatch):
    import asyncio
    import threading
    from camctl.host_files import downloads

    owned, ticket, intent, staging, work = original_read
    entered, release = threading.Event(), threading.Event()
    real_replace = downloads.os.replace

    def gate_cursor(source, destination, *args, **kwargs):
        result = real_replace(source, destination, *args, **kwargs)
        if Path(destination) == staging / '.read-download-cleanup-cursor':
            entered.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(downloads.os, 'replace', gate_cursor)
    task = asyncio.create_task(run_default_download_session(
        download_config, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert not work.exists()
        assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)


@pytest.mark.asyncio
async def test_unknown_prefix_never_hides_known_responsibility_across_runs(
        original_read, download_config, monkeypatch):
    owned, ticket, intent, staging, work = original_read
    unknown = work.parent / 'unknown-keep'
    unknown.mkdir()
    (unknown / 'keep').write_bytes(b'private unknown bytes')
    for _ in range(3):
        await run_download_maintenance(download_config,
            boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, limit=1)
        if not work.exists():
            break
    assert not work.exists(), '未知目录永久保留时，已知责任仍必须可达'
    assert (unknown / 'keep').read_bytes() == b'private unknown bytes'


@pytest.mark.asyncio
async def test_last_budget_original_ticket_waits_for_maintenance_then_resumes(
        photo_read_request, download_config, tmp_path, monkeypatch):
    from camctl.devices.read_session import ReadPreparationError, ReadPreparationSettlement
    from camctl.operations.models import AttemptStatus
    from ..devices import test_hasselblad_download
    from ..devices.test_hasselblad_download import DownloadTransport, RAW
    owned, command = photo_read_request
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
    ticket, intent = begin_read(owned, command, prepared, occurred_at=_NOW + 2_100_003)
    assert ticket.attempt_id == 2
    budgets = owned.connection.execute(
        "SELECT max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_attempts WHERE run_id=? ORDER BY attempt_no",
        (ticket.run_id,)).fetchall()
    assert [(maximum, Decimal(timeout), Decimal(retry)) for maximum, timeout, retry in budgets] == [
        (5, Decimal('23.000000000000000001'), Decimal('2.1'))] * 2
    staging = tmp_path / 'staging'
    earlier = make_work(staging, first, b'earlier settled residue')
    work = make_work(staging, ticket, b'old incomplete download')
    monkeypatch.setattr(test_hasselblad_download, 'NAME', 'B9996039.3FR')
    transport = DownloadTransport(serial='SERIAL-1')
    driver = HasselbladDriver({'cam-1': {'kind': 'camera', 'driver': DRIVER_ID,
        'hasselblad': {'executable': '/tool/hb_cli', 'serial': 'SERIAL-1', 'product_id': 10}}},
        BoundDirectories(staging), monotonic_ns=time.monotonic_ns, transport=transport)
    await run_download_maintenance(download_config,
        boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, limit=1)
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
        await run_download_maintenance(download_config,
            boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, limit=1)
        assert not work.exists()
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
@pytest.mark.parametrize('blocked', ['active', 'unconfirmed', 'late', 'delete_failure'])
async def test_bounded_cursor_advances_past_retained_or_failed_known_responsibility(
        two_photo_read_requests, download_config, tmp_path, monkeypatch, blocked):
    from camctl.host_files import downloads
    from camctl.host_files.downloads import load_download_cursor

    owned, first_command, second_command = two_photo_read_requests
    horizon, = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()
    first_grant = OutputsRepository().grant_file(first_command, new_operation_key(), owned)
    second_grant = OutputsRepository().grant_file(second_command, new_operation_key(), owned)
    assert first_grant.kind is DbOutcomeKind.COMPLETED, first_grant.error
    assert second_grant.kind is DbOutcomeKind.COMPLETED, second_grant.error
    first, first_intent = begin_read(owned, first_command, first_grant.value)
    second, second_intent = begin_read(owned, second_command, second_grant.value,
                                      device='cam-2', serial='SERIAL-2')
    assert first.run_id != second.run_id and first.target_id != second.target_id
    outcome = CallOutcome(effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('read_returned', 1, {})))
    saved = OperationRepository().finish_attempt(AttemptFinish(second,
        validate_outcome(second, outcome, EVIDENCE), _NOW + 20), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    staging = tmp_path / 'staging'
    first_work = make_work(staging, first)
    second_work = make_work(staging, second, b'completed residue')
    owner = downloads.DownloadWorkspace(staging, first) if blocked == 'active' else None
    if owner is not None:
        assert owner.claim()
    real_remove = downloads.shutil.rmtree
    failure = PermissionError(errno.EACCES, 'original directory delete denied', str(first_work))
    failures = []

    def remove(path):
        if path == first_work and blocked == 'delete_failure':
            failures.append(failure)
            raise failure
        return real_remove(path)

    monkeypatch.setattr(downloads.shutil, 'rmtree', remove)
    boundary = RecoveryBoundary.UNCONFIRMED if blocked == 'unconfirmed' else RecoveryBoundary.HOST_LOCAL_SETTLED
    try:
        if blocked == 'delete_failure':
            with pytest.raises(PermissionError) as caught:
                await run_download_maintenance(download_config, boundary=boundary, limit=1)
            assert caught.value is failure and failures == [failure]
        else:
            await run_download_maintenance(download_config, boundary=boundary, limit=1,
                                           horizon=horizon if blocked == 'late' else None)
        assert await load_download_cursor(staging) == first_intent
        assert first_work.exists() and second_work.exists()
        await run_download_maintenance(download_config, boundary=boundary, limit=1)
        assert await load_download_cursor(staging) == second_intent
        assert first_work.exists() and not second_work.exists()
        assert (first_work / 'partial').read_bytes() == b'partial'
        assert running_read_ticket(owned, int(first.target_id))[0] == first
        if owner is not None:
            owner.release()
        monkeypatch.setattr(downloads.shutil, 'rmtree', real_remove)
        await run_download_maintenance(download_config,
            boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, limit=1)
        assert not first_work.exists(), '游标完成一圈后必须重新核实曾保留的责任'
    finally:
        if owner is not None:
            owner.release()


@pytest.mark.asyncio
async def test_delete_failure_reaches_default_session_diagnostics(
        original_read, download_config, monkeypatch):
    import asyncio
    from camctl.host_files import downloads

    owned, ticket, intent, staging, work = original_read
    failure = PermissionError(errno.EACCES, 'original directory delete denied', str(work))
    failures = []
    real_remove = downloads.shutil.rmtree

    def remove(path):
        if path == work:
            failures.append(failure)
            raise failure
        return real_remove(path)

    monkeypatch.setattr(downloads.shutil, 'rmtree', remove)
    outcome = await asyncio.wait_for(run_default_download_session(
        download_config, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED), 15)
    assert_maintenance_failure(outcome, download_config, failure)
    assert failures == [failure]
    assert (work / 'partial').read_bytes() == b'partial'
    assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket


@pytest.mark.asyncio
async def test_cursor_write_failure_is_a_real_maintenance_error(
        original_read, download_config, monkeypatch):
    from camctl.host_files import downloads
    owned, ticket, intent, staging, work = original_read
    failure = PermissionError(errno.EACCES, 'cursor replace denied', str(staging / '.read-download-cleanup-cursor'))
    failures = []
    real_replace = downloads.os.replace
    def fail_replace(source, destination, *args, **kwargs):
        if Path(destination) == staging / '.read-download-cleanup-cursor':
            failures.append(failure)
            raise failure
        return real_replace(source, destination, *args, **kwargs)
    monkeypatch.setattr(downloads.os, 'replace', fail_replace)
    import asyncio
    outcome = await asyncio.wait_for(run_default_download_session(
        download_config, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED), 15)
    assert_maintenance_failure(outcome, download_config, failure)
    assert failures == [failure]
    assert not work.exists()
    assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket


@pytest.mark.asyncio
async def test_known_actual_owner_blocks_both_resume_and_unavailable_recovery(original_read):
    from camctl.host_files.downloads import DownloadWorkspace
    owned, ticket, intent, staging, work = original_read
    owner = DownloadWorkspace(staging, ticket)
    assert owner.claim()
    driver = HasselbladDriver({'cam-1': {'kind': 'camera', 'driver': DRIVER_ID,
        'hasselblad': {'executable': '/tool/hb_cli', 'serial': 'SERIAL-1', 'product_id': 10}}},
        BoundDirectories(staging), monotonic_ns=lambda: 0)
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
