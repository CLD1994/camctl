"""具体驱动装配测试经正式工厂、受理及业务保存边界准备场景。"""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.bootstrap.flows import cancel_flow
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .capture import capture_database, capture_factory, capture_runtime, restart_capture_database


async def dispatch_capture(owned, directory, driver, *, evidence, wall_us=None):
    """配置和证据在装配前提交，具体驱动仍执行真实命令编排。"""
    from ..capture.test_capture_contract import ResultsDouble
    from ..scheduling.test_start_action import _SCHEDULED
    driver_id, = owned.connection.execute('SELECT driver_id FROM actions WHERE id=1').fetchone()
    return await capture_runtime(owned, driver=driver, driver_id=driver_id,
        evidence=evidence, directory=directory, results=ResultsDouble({}),
        wall_us=(lambda: _SCHEDULED) if wall_us is None else wall_us,
        monotonic_ns=lambda: 0)


async def request_driver_cancel(owned, action_id, occurred_at, catalog):
    """从正式受理和取消流程使当前拍摄失去继续派发资格。"""
    database = await capture_database(owned)
    instant = datetime.fromtimestamp(occurred_at // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    accepted = await accept_input(ParsedInput('cancel-driver.json', {
        'request_id': '90', 'created_at': instant, 'name': '取消拍摄',
        'actions': [{'name': '取消', 'type': 'cancel_task',
            'params': {'target': {'action_instance_id': str(action_id)}}}]}),
        AcceptanceContext(CommandMode.RUN, catalog, AcceptanceRepository(),
            clock=SimpleNamespace(utc_micros=lambda: occurred_at)), new_operation_key(), database)
    assert accepted.plan_id is not None, accepted
    await cancel_flow(staging=Path(database.metadata.staging_path),
        ready=Path(database.metadata.ready_path),
        processing=Path(database.metadata.processing_path))(SimpleNamespace(database=database,
            clock=SimpleNamespace(utc_micros=lambda: occurred_at)))


async def reopen_driver_database(owned_or_path):
    """实际结束旧 worker 后重开同一个磁盘状态库。"""
    if isinstance(owned_or_path, (str, Path)):
        path = Path(owned_or_path)
    else:
        path = Path(owned_or_path.connection.execute('PRAGMA database_list').fetchone()[2])
        owned_or_path.connection.close()
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    await restart_capture_database(owned)
    return owned


class SavedGrant(Exception):
    """START意图真实提交，尚未派发设备命令。"""


async def interrupt_start_grant(runtime, action_id, handler, monkeypatch):
    """在正式处理器的真实授予事务完成后中断，返回原票据。"""
    from dataclasses import replace
    from camctl.capture.handlers import capture_handler
    method = SchedulingRepository.grant_start
    receipts = []
    def saved(repository, request, key, owned):
        receipt = method(repository, request, key, owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        assert receipt.value.ticket is not None, receipt.value
        receipts.append(receipt.value.ticket)
        return replace(receipt, handoff_error=SavedGrant())
    with monkeypatch.context() as boundary:
        boundary.setattr(SchedulingRepository, 'grant_start', saved)
        with pytest.raises(SavedGrant):
            await capture_handler(handler)(action_id, runtime)
    assert len(receipts) == 1
    return receipts[0]


async def driver_world_capture(world, **options):
    """具体已登记驱动与本次配置一起交给正式拍摄工厂。"""
    from camctl.capture.timelapse import CaptureWaitConfig
    factory_only = options.pop('factory_only', False)
    factory = await capture_factory(world.owned, driver=world.entry.driver,
        evidence=world.entry.evidence, drivers=options.pop('drivers', world.registry),
        devices=options.pop('devices', world.cfg.devices),
        staging=Path(world.cfg.paths.staging),
        wait_config=lambda action: CaptureWaitConfig(action['execution_spec_json']['target_duration_ms'], 30000, 1000),
        wall_us=lambda: world.wall[0], monotonic_ns=lambda: world.mono[0],
        media_enabled=options.pop('media_enabled', False), segment_size=options.pop('segment_size', 4),
        on_recovery_diagnostic=world.diagnostics.append, **options)
    return factory if factory_only else await factory(world.owned)


async def driver_world_obtain(world, *, reuse=False, **options):
    """正式取回工厂保留原保存责任，重开只换真实业务数据库。"""
    from camctl.bootstrap.obtain_assembly import session_obtain_assembly
    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.session.supervision import Supervisor
    if not reuse:
        supervisor = Supervisor()
        world.obtain_supervisors.append(supervisor)
        world.obtain_factory = session_obtain_assembly(
            devices=options.pop('devices', world.cfg.devices),
            drivers=options.pop('drivers', world.registry), staging=Path(world.cfg.paths.staging),
            ready=Path(world.cfg.paths.ready), processing=Path(world.cfg.paths.processing),
            segment_size=4, occurred_at=lambda: world.wall[0], monotonic_ns=lambda: world.mono[0],
            on_recovery_diagnostic=world.diagnostics.append,
            file_executor=FileTaskExecutor(supervisor), **options)
    return world.obtain_factory(await capture_database(world.owned))


async def close_driver_world(world):
    """先实际收场取回拥有的文件责任，再由共用作用域关闭数据库线程。"""
    for supervisor in world.obtain_supervisors:
        result = await supervisor.drain_required()
        assert not result.failed, result.failed
    world.obtain_supervisors.clear()
    world.owned.connection.close()


class SavedOperation(Exception):
    """正式处理器已保存目标意图或实际结果页。"""


async def interrupt_driver_result(world, monkeypatch, *, elapsed_us=11_000_000, pages='none'):
    """实际START/STOP后在RESULTS意图或首个真实页保存之后中断。"""
    from dataclasses import replace
    from camctl.capture.handlers import capture_handler
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.operations.attempts import OperationKind
    runtime = await driver_world_capture(world)
    if elapsed_us is not None:
        await capture_handler(world.kind)(1, runtime)
        world.wall[0] += elapsed_us
        world.mono[0] += elapsed_us * 1000
        if elapsed_us < 10_000_000:
            world.owned = await reopen_driver_database(world.owned)
            runtime = await driver_world_capture(world)
    if pages == 'partial':
        origin = world.channel.result_path
        world.channel.result_paths = [origin, *(origin.rsplit('/', 1)[0] + f'/unused-{index:02}.txt' for index in range(32))]
    world.channel.metadata_error = pages == 'terminal_error'
    async def elapsed(seconds):
        world.mono[0] += int(seconds * 1_000_000_000)
    monkeypatch.setattr('asyncio.sleep', elapsed)
    begun, saved = [], []
    begin = OperationRepository.begin_attempt
    save = CaptureRepository.save_result_page
    def begin_boundary(repository, intent, key, current):
        receipt = begin(repository, intent, key, current)
        if intent.kind is OperationKind.CHECK_CAPTURE_RESULTS:
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            begun.append(receipt.value.ticket)
            if pages == 'none':
                return replace(receipt, handoff_error=SavedOperation())
        return receipt
    def page_boundary(repository, request, key, current):
        receipt = save(repository, request, key, current)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        saved.append(repository.read_result_page(receipt.value, current))
        return replace(receipt, handoff_error=SavedOperation())
    with monkeypatch.context() as boundary:
        boundary.setattr(OperationRepository, 'begin_attempt', begin_boundary)
        if pages != 'none':
            boundary.setattr(CaptureRepository, 'save_result_page', page_boundary)
        with pytest.raises(SavedOperation):
            if elapsed_us is not None and elapsed_us < 10_000_000:
                from camctl.bootstrap.flows import winddown_flow
                async def same_runtime(database, device_id):
                    return runtime
                await winddown_flow(capture_factory=same_runtime, wait_cap_s=Decimal(0), sleep=elapsed)(
                    SimpleNamespace(database=runtime.database,
                        clock=SimpleNamespace(utc_micros=lambda: world.wall[0])))
            else:
                await capture_handler(world.kind)(1, runtime)
    assert len(begun) == 1
    world.saved_page = None if not saved else saved[0]
    record_driver_state(world, "result_intent_or_first_page_saved")
    return runtime, begun[0]


async def interrupt_stop_grant(runtime, action_id, handler, monkeypatch):
    """正式取消或正常到时流程已保存STOP意图，尚未派发。"""
    from dataclasses import replace
    from camctl.capture.handlers import capture_handler
    from camctl.operations.attempts import OperationKind
    from camctl.persistence.repositories.operations import OperationRepository
    begin = OperationRepository.begin_attempt
    tickets = []
    def saved(repository, request, key, owned):
        receipt = begin(repository, request, key, owned)
        if request.kind is OperationKind.STOP:
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            tickets.append(receipt.value.ticket)
            return replace(receipt, handoff_error=SavedOperation())
        return receipt
    with monkeypatch.context() as fault:
        fault.setattr(OperationRepository, 'begin_attempt', saved)
        with pytest.raises(SavedOperation):
            await capture_handler(handler)(action_id, runtime)
    assert len(tickets) == 1
    return tickets[0]


async def recover_through_capture(world, runtime, ticket, monkeypatch, *, factory=None):
    """正式消费者发现原恢复，在真实 UNKNOWN 保存后观察独立事实。"""
    from dataclasses import replace
    from camctl.capture.handlers import capture_handler
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.operations import OperationRepository
    finish = OperationRepository.finish_attempt
    finish_start = CaptureRepository.finish_start_result
    saved = []
    def receipt_for(request, key, receipt):
        if request.ticket == ticket:
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            saved.append((request, key))
            return replace(receipt, handoff_error=SavedOperation())
        return receipt
    def observed(repository, request, key, owned):
        return receipt_for(request, key, finish(repository, request, key, owned))
    def observed_start(repository, request, observation, key, owned, **options):
        return receipt_for(request, key, finish_start(repository, request, observation, key, owned, **options))
    with monkeypatch.context() as fault:
        fault.setattr(OperationRepository, 'finish_attempt', observed)
        fault.setattr(CaptureRepository, 'finish_start_result', observed_start)
        record_driver_state(world, 'before_original_attempt_recovery')
        with pytest.raises(SavedOperation):
            try:
                if ticket.operation == 'stop' and factory is not None:
                    from camctl.bootstrap.flows import residual_flow
                    await residual_flow(factory)(SimpleNamespace(database=runtime.database,
                        clock=SimpleNamespace(utc_micros=lambda: world.wall[0])))
                else:
                    await capture_handler(world.kind)(1, runtime)
            finally:
                record_driver_state(world, 'after_original_attempt_recovery')
    assert len(saved) == 1
    return saved[0]


async def interrupt_read_intent(world, runtime, monkeypatch, *, internal):
    """由原消费者正式重试读取，第二张票据提交后中断。"""
    from dataclasses import replace
    from camctl.capture.handlers import capture_handler
    from camctl.outputs.obtain_flow import advance_obtain
    from camctl.operations.attempts import OperationKind
    from camctl.persistence.repositories.operations import OperationRepository
    begin = OperationRepository.begin_attempt
    tickets = []
    def saved(repository, request, key, owned):
        receipt = begin(repository, request, key, owned)
        if request.kind is OperationKind.READ_FILE:
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            assert receipt.value.ticket is not None, receipt.value
            tickets.append(receipt.value.ticket)
            return replace(receipt, handoff_error=SavedOperation())
        return receipt
    with monkeypatch.context() as fault:
        fault.setattr(OperationRepository, 'begin_attempt', saved)
        with pytest.raises(SavedOperation):
            if internal:
                for _ in range(4):
                    await capture_handler(world.kind)(1, runtime)
            else:
                await advance_obtain(runtime)
    assert len(tickets) == 1
    return tickets[0]


@pytest.fixture
def driver_owned(tmp_path):
    """显式初始化拥有完整工作目录和当前路径绑定的真实状态库。"""
    from camctl.bootstrap.config import ConfigDefaults, load_config
    from camctl.persistence.initialization import initialize_state, InitOutcome
    config = load_config({'paths': {name: str(tmp_path / file) for name, file in (
        ('state_db', 'state.db'), ('log_file', 'camctl.log'), ('staging', 'staging'),
        ('ready', 'ready'), ('processing', 'processing'))}}, ConfigDefaults())
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    owned = open_existing(Path(config.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        yield owned
    finally:
        owned.connection.close()


async def stop_driver_capture(world, monkeypatch, *, elapsed_us):
    """正式正常到时STOP可靠完成，在RESULTS前的活动结束事务后中断。"""
    from dataclasses import replace
    from camctl.capture.handlers import capture_handler
    from camctl.persistence.repositories.capture import CaptureRepository
    runtime = await driver_world_capture(world)
    await capture_handler(world.kind)(1, runtime)
    world.wall[0] += elapsed_us
    world.mono[0] += elapsed_us * 1000
    conclude = CaptureRepository.conclude_activity
    saved = []
    def interrupted(repository, request, key, owned):
        receipt = conclude(repository, request, key, owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        saved.append((request, key))
        return replace(receipt, handoff_error=SavedOperation())
    with monkeypatch.context() as fault:
        fault.setattr(CaptureRepository, 'conclude_activity', interrupted)
        with pytest.raises(SavedOperation):
            await capture_handler(world.kind)(1, runtime)
    assert len(saved) == 1
    return runtime


def driver_media_tools(directory):
    """通过受管工具端口提供真实进程及真实修复文件，保留媒体解析链。"""
    from camctl.host_files.media import ProbeRequest, RepairRequest
    from ..bootstrap.test_media_assembly import _tool
    log = Path(directory) / 'media-calls.jsonl'
    probe = _tool(Path(directory), 'driver-probe', '''from pathlib import Path
import json,sys
with Path(%r).open('a') as log: log.write('probe\\n')
output = sys.argv[-1].lower().endswith('.mp4')
seconds = '10' if output else '75'
streams = [{'index':i,'codec_type':'video','codec_name':'hevc','width':3840,'height':3840,
    'duration':seconds,'disposition':{'attached_pic':0}} for i in (0,2)]
streams.append({'index':3,'codec_type':'audio','codec_name':'aac','duration':seconds,'disposition':{'attached_pic':0}})
if not output: streams.append({'index':1,'codec_type':'video','codec_name':'mjpeg','width':320,'height':180,'disposition':{'attached_pic':1}})
print(json.dumps({'format':{'duration':seconds,'format_name':'mov,mp4,m4a,3gp,3g2,mj2'},'streams':streams}))
''' % str(log))
    repair = _tool(Path(directory), 'driver-repair', '''from pathlib import Path
import sys
with Path(%r).open('a') as log: log.write('repair\\n')
Path(sys.argv[-1]).write_bytes(b'complete repaired video')
''' % str(log))
    return {'probe_request': ProbeRequest(ffprobe=probe), 'repair_request': RepairRequest(ffmpeg=repair)}, log


def record_driver_state(world, stage):
    """按实际业务阶段归档有限场景的数据库事实，供迁移失败交接。"""
    import json
    tables = {}
    for table in ('actions', 'device_activities', 'recording_processing', 'operation_runs',
                  'operation_attempts', 'device_files', 'file_copies'):
        cursor = world.owned.connection.execute('SELECT * FROM ' + table + ' ORDER BY id')
        columns = [column[0] for column in cursor.description]
        tables[table] = [dict(zip(columns, row)) for row in cursor]
    cursor = world.owned.connection.execute('SELECT id,transaction_id,occurred_at,body_json FROM history_events'
        ' WHERE id IN (SELECT result_first_page_event_id FROM operation_attempts'
        ' UNION SELECT result_last_page_event_id FROM operation_attempts) ORDER BY id')
    tables['actual_result_pages'] = [{'id': identity, 'transaction_id': transaction,
        'occurred_at': occurred_at, 'body': json.loads(body)} for identity, transaction, occurred_at, body in cursor]
    states = getattr(world, 'flow_states', None)
    if states is None:
        world.flow_states = states = []
    states.append({'stage': stage, 'wall_us': world.wall[0], 'monotonic_ns': world.mono[0],
        'last_history_event': world.owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0],
        'tables': tables})
    (Path(world.cfg.paths.staging).parent / 'driver-flow.json').write_text(
        json.dumps(states, ensure_ascii=False, indent=2) + '\n')
    return states[-1]
