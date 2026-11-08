"""清理和取消专项共用正式工厂、仓储命令与实际资源边界。"""

import json
from contextlib import asynccontextmanager

from camctl.cancellation.models import (
    ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixCancelTargets,
    FixedCancelSet, FixedTarget, SelectionBasis, StartCancelAction,
)
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository


def apply_direct_cancel(owned, origin, occurred_at, *, mode):
    """取消事务专项固定原请求明确指定的一个动作，再正式施加效果。"""
    fields, = owned.connection.execute('SELECT input_fields_json FROM actions WHERE id=?', (origin,)).fetchone()
    target = int(json.loads(fields)['params']['target']['action_instance_id'])
    status, = owned.connection.execute('SELECT status FROM actions WHERE id=?', (target,)).fetchone()
    repository = CancellationRepository()
    started = repository.start_cancel_action(StartCancelAction(origin, occurred_at), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cancel_targets(FixCancelTargets(origin, FixedCancelSet((FixedTarget(
        target, SelectionBasis.DIRECT, CancellationEffect.NOT_REQUIRED if status in (3,4,5,6)
        else CancellationEffect.NOT_APPLIED),)), occurred_at), new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    assert len(fixed.value.item_ids) == 1
    key = new_operation_key()
    applied = repository.apply_cancel_target(ApplyCancelTarget(fixed.value.item_ids[0], mode, occurred_at), key, owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    return key, fixed.value.item_ids[0], target


def cleanup_case_factory(*, devices, drivers, staging, file_executor, occurred_at,
                         max_delete_attempts=3, max_query_attempts=2, **options):
    """正式装配持有同会话原申请；观察连接只用于取得所属数据库路径。"""
    from .outputs import cleanup_factory
    from .capture import capture_database
    assembly = cleanup_factory(devices=devices, drivers=drivers, staging=staging,
        file_executor=file_executor, occurred_at=occurred_at, monotonic_ns=lambda: 0,
        max_delete_attempts=max_delete_attempts, max_query_attempts=max_query_attempts, **options)
    async def factory(owned_or_database):
        return await assembly(await capture_database(owned_or_database))
    return factory


def save_cleanup_intent(owned, item_id, operation, config, occurred_at):
    """原票据恢复专项经公开共同建档事务建立尚未保存实际结果的责任。"""
    from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
    from camctl.outputs.batches import BatchOutcome, BatchRequest
    from camctl.persistence.repositories.file_batches import FileBatchRepository
    from camctl.persistence.repositories.outputs import OutputsRepository
    action, device = owned.connection.execute(
        'SELECT c.action_id,a.device_id FROM cleanup_items c'
        ' JOIN outputs o ON o.id=COALESCE(c.output_id,c.requested_output_id)'
        ' LEFT JOIN device_files f ON f.id=o.device_file_id'
        ' LEFT JOIN actions a ON a.id=f.observer_action_id WHERE c.id=?', (item_id,)).fetchone()
    if device is not None:
        batch = FileBatchRepository().acquire(BatchRequest(action, device, occurred_at), new_operation_key(), owned)
        assert batch.kind is DbOutcomeKind.COMPLETED and batch.value in (BatchOutcome.GRANTED, BatchOutcome.HELD), batch
    intent = AttemptIntent(operation, action,
        OperationKind.DELETE_FILE if operation == 'delete' else OperationKind.CHECK_FILE_EXISTS,
        AttemptTarget(cleanup_item_id=item_id), None, config, occurred_at)
    key = new_operation_key()
    saved = OutputsRepository().begin_cleanup_attempt(intent, key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert saved.value.ticket is not None
    horizon, = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()
    return saved.value.ticket, horizon


def fix_obtain_selections(owned, target, occurred_at):
    """选择事务专项正式开始并解析来源，逐成员固定公开选择快照。"""
    from camctl.outputs.sources import SourceResolution, ResolutionState, read_source_spec, select_outputs
    from camctl.outputs.definitions import read_selection_request, read_excluded_output_ids
    from camctl.persistence.repositories.outputs import OutputsRepository, StartObtainAction, ResolveSources, FixSelection, load_selection_facts
    repository = OutputsRepository()
    fields, execution = owned.connection.execute('SELECT input_fields_json,execution_spec_json FROM actions WHERE id=?', (target,)).fetchone()
    for method, command in ((repository.start_obtain_action, StartObtainAction(target, occurred_at)),
        (repository.resolve_sources, ResolveSources(target, read_source_spec(fields), occurred_at))):
        saved = method(command, new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    mode, requested = read_selection_request(json.loads(execution), json.loads(fields))
    rows = owned.connection.execute('SELECT s.id,d.depends_on_action_id FROM obtain_source_selections s JOIN action_dependencies d ON d.id=s.dependency_id WHERE d.action_id=? ORDER BY s.id', (target,)).fetchall()
    source_plan, = owned.connection.execute('SELECT resolved_source_plan_id FROM actions WHERE id=?', (target,)).fetchone()
    for selection, source in rows:
        snapshot = select_outputs(SourceResolution(ResolutionState.FIXED,
            source_plan_id=source_plan, member_action_ids=(source,)),
            load_selection_facts(owned.connection, source, requested_output_ids=requested), mode, requested,
            excluded_output_ids=read_excluded_output_ids(json.loads(fields)))
        fixed = repository.fix_selection(FixSelection(selection, snapshot, occurred_at), new_operation_key(), owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error


@asynccontextmanager
async def download_obtain_runtime(owned, staging, driver, *, boundary, horizon,
                                  configured=False, max_attempts=5):
    """正式取回工厂解析原下载恢复能力，并使用实际文件任务和数据库线程。"""
    from decimal import Decimal
    from pathlib import Path
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    from camctl.devices.drivers.hasselblad.driver import EVIDENCE
    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.session.supervision import Supervisor
    from .capture import capture_database
    from .outputs import obtain_factory, output_drivers
    staging = Path(staging)
    for name in ('deliveries', 'recording-inputs', 'derived'):
        (staging / name).mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor()
    factory = obtain_factory(devices=({'cam-1': {'driver': DRIVER_ID, 'copy': {
        'max_read_attempts': max_attempts, 'retry_interval_s': Decimal(0),
        'read_idle_timeout_s': Decimal('10')}}} if configured else {}),
        drivers=output_drivers(driver, EVIDENCE, driver_id=DRIVER_ID, read=True,
            local_read_recovery=True),
        staging=staging, ready=staging.parent / 'ready', processing=staging.parent / 'processing',
        segment_size=5, occurred_at=lambda: 1_750_000_000_000_020, monotonic_ns=lambda: 0,
        recovery_boundary=boundary, recovery_max_event_id=lambda: horizon,
        file_executor=FileTaskExecutor(supervisor))
    try:
        yield factory(await capture_database(owned))
    finally:
        result = await supervisor.drain_required()
        assert not result.failed, result.failed


async def run_download_maintenance(config, monkeypatch, *, boundary, limit=10, after_range=None):
    """默认会话固定范围并完整完成本轮维护；在后续业务读取前交还观察权。

    只有整页所有游标均实际保存后才停止，避免首项完成就掩盖超额处理。
    文件错误原样抛给会话，错误场景等待真实命令结果和诊断关闭。
    """
    import asyncio
    from dataclasses import replace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap import download_cleanup
    from camctl.bootstrap.lifecycle import build_runtime, execute_command, close_runtime
    from camctl.persistence.business import BusinessDatabase

    class BatchObserved(asyncio.CancelledError):
        pass

    config = replace(config, cleanup=replace(config.cleanup,
        work_file_batch_size=1, work_file_limit_per_run=limit))
    deps = build_runtime(CommandMode.RUN, config, recovery_boundary=boundary)
    actual_read = BusinessDatabase.read
    actual_save = download_cleanup.save_download_cursor
    actual_recover = download_cleanup.recover_download
    observed = {'candidates': False, 'expected': None, 'batch_done': False,
        'cursor_writes': [], 'stopped_after_batch': False, 'errors': []}

    async def save(staging, after):
        try:
            await actual_save(staging, after)
        except BaseException as error:
            observed['errors'].append(error)
            raise
        observed['cursor_writes'].append(after)
        observed['batch_done'] = len(observed['cursor_writes']) == observed['expected']

    async def recover(staging, ticket):
        try:
            return await actual_recover(staging, ticket)
        except BaseException as error:
            observed['errors'].append(error)
            raise

    async def read(database, callback, *, description='业务读取'):
        if observed['batch_done'] and not observed['stopped_after_batch'] and not observed['errors']:
            observed['stopped_after_batch'] = True
            raise BatchObserved()
        value = await actual_read(database, callback, description=description)
        if description == '中间文件初始范围读取' and after_range is not None:
            await after_range(database)
        if description == 'READ下载遗留候选读取':
            observed['candidates'] = True
            observed['expected'] = len(value)
            observed['batch_done'] = not value
        return value

    with monkeypatch.context() as patch:
        patch.setattr(BusinessDatabase, 'read', read)
        patch.setattr(download_cleanup, 'save_download_cursor', save)
        patch.setattr(download_cleanup, 'recover_download', recover)
        try:
            try:
                observed['outcome'] = await asyncio.wait_for(
                    execute_command(deps, None, poll_interval_s=0.001), timeout=10)
            except BatchObserved:
                observed['outcome'] = None
            assert observed['candidates'], '默认会话尚未进入下载遗留候选读取'
            assert len(observed['cursor_writes']) <= limit, '完整一轮维护不得越过部署额度'
            return observed
        finally:
            close_runtime(deps)
