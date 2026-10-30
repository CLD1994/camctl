"""OSV 准备结束后才能为原输入和唯一输出取得完整修复资格。"""
from decimal import Decimal

import pytest

from camctl.capture.media_flow import load_processing_status, run_recording_media
from camctl.host_files.media import MediaProbe, MediaStream
from camctl.host_files.tasks import AsyncFileTask, FileTaskExecutor, FileTaskId
from camctl.session.supervision import Supervisor

from .media_retry_fixtures import media_pipeline as pipeline
from ..support.capture_media_cases import (
    ScriptTools, assembled_media, handler_runtime as assembled_handler, reopen_media, media_case, media_cases,
)

pytestmark = pytest.mark.asyncio


def source_probe():
    return MediaProbe(Decimal('75'), None, (
        MediaStream(0, 'video', 'h264', 64, 64, Decimal('75'), False),
        MediaStream(1, 'video', 'h264', 64, 64, Decimal('75'), False),
    ), 'mov,mp4,m4a,3gp,3g2,mj2')


class _Owner:
    async def take_over(self, task):
        await task.pending.wait()


class Tools(ScriptTools):
    def __init__(self, executor):
        super().__init__(source_probe())
        self.executor = executor


async def media_flow(pipeline, executor, tools):
    return await assembled_media(pipeline, executor, tools)


@pytest.mark.parametrize('pipeline', ['osv'], indirect=True)
async def test_pending_osv_returns_prepared_after_releasing_input_owner(pipeline):
    owned, _roots, source_id = pipeline
    executor = FileTaskExecutor(Supervisor())
    tools = Tools(executor)
    step = await run_recording_media(await media_flow(pipeline, executor, tools), 1, 1, source_id,
        target_extension='osv')
    assert step.phase.value == 'prepared'
    status = load_processing_status(owned, 1)
    assert status.repair_state == 4 and status.source_probe == source_probe()
    assert status.repair_output_file_id == step.output_file.file_id
    assert owned.connection.execute('SELECT COUNT(*) FROM intermediate_files WHERE purpose=4').fetchone() == (1,)
    assert len(tools.probes) == 1 and tools.repairs == []
    assert executor.unfinished_files() == ()


@pytest.mark.parametrize('pipeline', ['osv'], indirect=True)
async def test_failed_read_waits_three_seconds_on_session_clock(pipeline):
    from camctl.capture.input_copy import InputPhase
    from camctl.devices.read_session import ReadSession
    from .test_media_flow import _FailingStream

    owned, _roots, source_id = pipeline
    executor = FileTaskExecutor(Supervisor())
    flow = await media_flow(pipeline, executor, Tools(executor))
    case = media_case(flow)
    opens = []

    async def failing_read(source, offset, ticket, *, idle_timeout_s):
        opens.append(offset)
        return ReadSession(source, offset, _FailingStream(), idle_timeout_s)

    case.driver.open_read = failing_read
    first = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    assert first.phase is InputPhase.SEGMENT_FAILED
    assert owned.connection.execute(
        'SELECT max_attempts_used, timeout_s_json, retry_interval_s_json, retry_wait_required '
        'FROM operation_runs WHERE kind=3').fetchone() == (3, '10', '3', 1)
    case.mono[0] += 2_999_999_999
    waiting = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    assert waiting.phase is InputPhase.RETRY_WAITING and len(opens) == 1
    case.mono[0] += 1
    retried = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    assert retried.phase is InputPhase.SEGMENT_FAILED and len(opens) == 2


# 观察器只记录实际受理和结束；所有裁决仍由原执行器执行。
class ObservedExecutor(FileTaskExecutor):
    def __init__(self):
        super().__init__(Supervisor())
        self.events = []
        self.results = []

    async def run_async_file_task(self, task, owner):
        from dataclasses import replace
        async def actual(control):
            self.events.append(('admit', task.stage, task.file_ids, task.task_id))
            return await task.body(control)
        if task.stage == 'media_probe' and getattr(self, 'prevent_probe', False):
            from camctl.host_files.tasks import FileTaskResult
            return FileTaskResult(task.task_id, ran=False)
        result = await super().run_async_file_task(replace(task, body=actual), owner)
        self.results.append((task.stage, result))
        self.events.append(('finish', task.stage, self.unfinished_files(), task.task_id))
        self.events.append(('delivered', task.stage, self.unfinished_files(), task.task_id))
        return result


class ControlledTools(ScriptTools):
    def __init__(self, executor, *, probe=None, result='success', probe_ran=True):
        super().__init__(source_probe() if probe is None else probe, result=result)
        self.executor, self.probe_ran = executor, probe_ran
        executor.prevent_probe = not probe_ran


@pytest.mark.parametrize('pipeline', ['osv-timing', 'osv'], indirect=True)
@pytest.mark.parametrize('result', ['success', 'failed', 'output_probe_failed'])
async def test_two_stages_reuse_saved_probe_and_output_with_complete_ownership(pipeline, result):
    owned, _roots, source_id = pipeline
    executor = ObservedExecutor()
    tools = ControlledTools(executor, result=result)
    flow = await media_flow(pipeline, executor, tools)
    first = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    assert first.phase.value == 'prepared'
    input_id, = tools.probes
    assert tools.repairs == [] and executor.unfinished_files() == ()
    status = load_processing_status(owned, 1)
    assert status.source_probe == source_probe()
    assert status.repair_output_file_id == first.output_file.file_id
    second = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    assert second.phase.value == ('succeeded' if result == 'success' else 'failed_saved')
    assert tools.repairs == [first.output_file.file_id]
    assert tools.probes.count(input_id) == 1
    assert executor.unfinished_files() == ()
    assert owned.connection.execute('SELECT COUNT(*) FROM intermediate_files WHERE purpose=4').fetchone() == (1,)
    prepare = [e for e in executor.events if e[1] == 'media_repair_prepare']
    repair = [e for e in executor.events if e[1] == 'media_repair']
    assert prepare[0][:3] == ('admit', 'media_repair_prepare', (input_id,))
    finished = next(e for e in prepare if e[0] == 'finish')
    assert finished[:3] == ('finish', 'media_repair_prepare', ())
    assert prepare[-1][:3] == ('delivered', 'media_repair_prepare', ())
    assert repair[0][:3] == ('admit', 'media_repair', (input_id, first.output_file.file_id))
    assert executor.events.index(prepare[-1]) < executor.events.index(repair[0])
    assert prepare[0][3] != repair[0][3]
    before = tuple(owned.connection.iterdump())
    await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    assert tuple(owned.connection.iterdump()) == before
    assert len(tools.repairs) == 1


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
@pytest.mark.parametrize('fact', ['not_run', 'tool_failed', 'missing_duration', 'missing_video'])
async def test_prepare_preserves_unusable_or_missing_actual_probe(pipeline, fact):
    from dataclasses import replace
    probe = source_probe()
    if fact == 'tool_failed': probe = MediaProbe(None, 'tool_failed: exit=1')
    if fact == 'missing_duration': probe = replace(probe, duration_s=None, error='missing_duration: 缺少时长字段')
    if fact == 'missing_video': probe = replace(probe, streams=probe.streams[:1])
    executor = ObservedExecutor()
    tools = ControlledTools(executor, probe=probe, probe_ran=fact != 'not_run')
    flow = await media_flow(pipeline, executor, tools)
    first = await run_recording_media(flow, 1, 1, pipeline[2], target_extension='osv')
    if fact == 'not_run':
        assert first.phase.value == 'tool_not_completed'
        assert pipeline[0].connection.execute('SELECT COUNT(*) FROM intermediate_files WHERE purpose=4').fetchone() == (0,)
    else:
        assert first.phase.value == 'prepared'
        assert load_processing_status(pipeline[0], 1).source_probe == probe
        second = await run_recording_media(flow, 1, 1, pipeline[2], target_extension='osv')
        assert second.phase.value == 'failed_saved'
        assert len(tools.probes) == 1
    assert tools.repairs == [] and executor.unfinished_files() == ()


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
@pytest.mark.parametrize('stage,result', [('start_repair_output', 'success'),
    ('complete_repair_output', 'success'), ('save_repair_result', 'failed'),
    ('save_repair_result', 'output_probe_failed')])
@pytest.mark.parametrize('fault', ['rollback', 'commit_before', 'commit_after'])
async def test_original_stage_request_recovers_on_new_connection_without_repeating_tools(
        pipeline, monkeypatch, stage, result, fault):
    from dataclasses import replace
    from pathlib import Path
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from ..bootstrap.test_binding_transactions import _CommitFailure
    owned, roots, source_id = pipeline
    executor = ObservedExecutor()
    tools = ControlledTools(executor, result=result)
    flow = await media_flow(pipeline, executor, tools)
    if stage != 'start_repair_output':
        prepared = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
        assert prepared.phase.value == 'prepared'
    original = getattr(CaptureRepository, stage)
    requests, outcomes = [], []
    def save(repo, command, key, current):
        requests.append((command, key))
        expected_files = (tools.probes[0],) if stage == 'start_repair_output' else (
            tools.probes[0], tools.repairs[0])
        if len(requests) == 1:
            assert executor.unfinished_files() == expected_files
        if len(requests) == 1:
            connection = (__import__('integration.support.output_planning', fromlist=['storage_fault']).storage_fault(current.connection, 'UPDATE recording_processing')
                if fault == 'rollback' else _CommitFailure(current.connection, fault == 'commit_after'))
            current = replace(current, connection=connection)
        outcome = original(repo, command, key, current)
        outcomes.append(outcome)
        return outcome
    monkeypatch.setattr(CaptureRepository, stage, save)
    with pytest.raises(ConsistencyError):
        await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    assert len(requests) == 1
    assert outcomes[0].kind is (DbOutcomeKind.ROLLED_BACK if fault == 'rollback' else DbOutcomeKind.UNKNOWN)
    probes, repairs = list(tools.probes), list(tools.repairs)
    path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    owned.connection.close()
    reopened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        # 保存恢复入口先完成原申请；当前时刻不会参与重算原命令。
        flow = await reopen_media(flow, reopened)
        media_case(flow).wall[0] = 1_750_000_200_000_000
        await media_case(flow).resume_saves()
        saved_calls = len(requests)
        await media_case(flow).resume_saves()
        assert len(requests) == saved_calls
        assert requests == [requests[0], requests[0]]
        assert outcomes[1].kind is DbOutcomeKind.COMPLETED, outcomes[1].error
        if fault == 'commit_after': assert outcomes[1].value.disposition.value == 'already'
        assert tools.probes == probes and tools.repairs == repairs
        status = load_processing_status(reopened, 1)
        assert status.source_probe == source_probe()
        assert reopened.connection.execute('SELECT COUNT(*) FROM intermediate_files WHERE purpose=4').fetchone() == (1,)
        if stage == 'start_repair_output':
            media_case(flow).wall[0] = 1_750_000_200_000_000
            done = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
            assert done.phase.value == 'succeeded'
            assert tools.probes.count(probes[0]) == 1 and len(tools.repairs) == 1
        assert executor.unfinished_files() == ()
    finally:
        reopened.connection.close()


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
async def test_registered_output_survives_real_external_file_conflict(pipeline):
    import asyncio
    from camctl.host_files.tasks import FileTaskError
    owned, _roots, source_id = pipeline
    executor = ObservedExecutor()
    tools = ControlledTools(executor)
    flow = await media_flow(pipeline, executor, tools)
    first = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    input_id = tools.probes[0]
    started, release = asyncio.Event(), asyncio.Event()
    async def conflict(_control):
        started.set()
        await release.wait()
    task = asyncio.create_task(executor.run_owned_async_file_task(AsyncFileTask(
        FileTaskId('external-reader'), (input_id,), 'external', '真实冲突', conflict)))
    try:
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(FileTaskError):
            await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
        assert tools.repairs == []
        assert load_processing_status(owned, 1).repair_output_file_id == first.output_file.file_id
    finally:
        release.set()
        await task
    result = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    assert result.phase.value == 'succeeded' and tools.repairs == [first.output_file.file_id]
    assert len(tools.probes) == 2 and executor.unfinished_files() == ()


async def test_registered_queued_file_task_is_withdrawn_and_identity_reused():
    import asyncio
    from camctl.host_files.tasks import FileTask

    reached, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def runner(fn):
        reached.set()
        await release.wait()
        return await asyncio.to_thread(fn)

    executor = FileTaskExecutor(Supervisor(), thread_runner=runner)
    task = FileTask(FileTaskId('queued-prepare'), 1, 'media_repair_prepare',
        '录像修复准备', lambda _stop: calls.append('executed'))
    waiting = asyncio.create_task(executor.run_file_task(task, _Owner()))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        lease = executor.lease_of(task.task_id)
        assert lease is not None and not lease.acquired
        assert executor.unfinished_files() == (1,)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert lease.released and calls == []
        assert executor.unfinished_files() == ()
        release.set()
        result = await executor.run_file_task(task, _Owner())
        assert result.ran and calls == ['executed']
        assert executor.unfinished_files() == ()
        assert not (await executor.drain_required()).failed
    finally:
        release.set()
        await asyncio.gather(waiting, return_exceptions=True)


class GatedExecutor(ObservedExecutor):
    def __init__(self, phase):
        import asyncio
        super().__init__()
        self.phase, self.reached, self.release = phase, asyncio.Event(), asyncio.Event()

    async def run_async_file_task(self, task, owner):
        if task.stage != 'media_repair_prepare':
            return await super().run_async_file_task(task, owner)
        if self.phase == 'before_prepare_dispatch':
            self.reached.set()
            await self.release.wait()
        result = await super().run_async_file_task(task, owner)
        if self.phase == 'prepared_result_before_return':
            self.reached.set()
            await self.release.wait()
        return result


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
@pytest.mark.parametrize('phase', ['before_prepare_dispatch', 'probe_result_before_save', 'prepared_result_before_return'])
async def test_waiter_cancel_preserves_preparation_results_and_blocks_repair(pipeline, phase, monkeypatch):
    import asyncio
    executor = GatedExecutor(phase)
    tools = ControlledTools(executor)
    if phase == 'probe_result_before_save':
        from ..support.capture_media_cases import MediaSaveGate
        saved = MediaSaveGate(monkeypatch, 'start_repair_output')
        reached, release = saved.reached, saved.release
    else:
        reached, release = executor.reached, executor.release
    flow = await media_flow(pipeline, executor, tools)
    running = asyncio.create_task(run_recording_media(flow, 1, 1, pipeline[2], target_extension='osv'))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        if phase == 'probe_result_before_save':
            request, _key = saved.requests[0]
            actual = request.source_probe.tool_diagnostics.outcome
            assert actual.error is None and actual.exit.exit_code == 0
        running.cancel()
        # 让取消到达原等待边界；不以 sleep 时长制造时序。
        for _ in range(5): await asyncio.sleep(0)
        if phase == 'probe_result_before_save':
            assert not running.done() and executor.unfinished_files()
        release.set()
        with pytest.raises(asyncio.CancelledError): await running
        status = load_processing_status(pipeline[0], 1)
        if phase == 'probe_result_before_save':
            from ..support.capture_media_cases import write_tool_evidence
            write_tool_evidence(pipeline[1].staging.parent, executor)
        assert status.repair_state == (3 if phase == 'before_prepare_dispatch' else 4)
        assert status.source_probe == (None if phase == 'before_prepare_dispatch' else source_probe())
        assert len(tools.probes) == (0 if phase == 'before_prepare_dispatch' else 1)
        assert tools.repairs == [] and executor.unfinished_files() == ()
        assert pipeline[0].connection.execute('SELECT cancel_requested FROM actions WHERE id=1').fetchone() == (0,)
        settled = await executor.drain_required()
        assert not settled.failed
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)


async def handler_runtime(pipeline, flow):
    return assembled_handler(flow)


def business_cancel(owned):
    from datetime import datetime, timezone
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
    from camctl.persistence.repositories.cancellation import register_cancellation_guards
    from .media_retry_fixtures import _Catalog
    from .test_input_copy import _NOW
    from .test_host_photo_flow import _apply_cancel
    register_cancellation_guards()
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    result = AcceptanceRepository().process_input(ProcessInput(ParsedInput('cancel.json', {
        'request_id': '9', 'created_at': instant, 'name': '取消录像',
        'actions': [{'name': '取消', 'type': 'cancel_task',
            'params': {'target': {'action_instance_id': '1'}}}]}), _Catalog(), CommandMode.RUN, _NOW),
        new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    cancel_id, = owned.connection.execute('SELECT id FROM actions WHERE plan_id=?', (result.value.plan_id,)).fetchone()
    _apply_cancel(owned, cancel_id, 1)


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
async def test_real_handler_continues_prepared_stage_without_new_request(pipeline):
    from camctl.capture.handlers import capture_handler
    owned = pipeline[0]
    executor = ObservedExecutor()
    tools = ControlledTools(executor)
    flow = await media_flow(pipeline, executor, tools)
    runtime = (await handler_runtime(pipeline, flow))
    advance = capture_handler('camera_record')
    await advance(1, runtime)
    assert owned.connection.execute('SELECT status FROM actions WHERE id=1').fetchone() == (2,)
    assert load_processing_status(owned, 1).repair_state == 4
    assert tools.repairs == []
    assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
    await advance(1, runtime)
    assert owned.connection.execute('SELECT status FROM actions WHERE id=1').fetchone() == (3,)
    assert len(tools.repairs) == 1
    assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (2,)
    assert executor.unfinished_files() == ()
    await advance(1, runtime)
    assert len(tools.repairs) == 1


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
@pytest.mark.parametrize('phase', ['probe', 'between', 'repair'])
async def test_business_cancel_keeps_actual_stage_result_and_blocks_new_content(pipeline, phase):
    import asyncio
    from camctl.capture.handlers import capture_handler
    from dataclasses import replace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
    from .media_retry_fixtures import _Catalog
    owned, roots, _source = pipeline
    executor = ObservedExecutor()
    tools = ControlledTools(executor)
    flow = await media_flow(pipeline, executor, tools)
    runtime = (await handler_runtime(pipeline, flow))
    advance = capture_handler('camera_record')
    if phase in ('between', 'repair'):
        await advance(1, runtime)
        assert load_processing_status(owned, 1).repair_state == 4
    if phase == 'between':
        business_cancel(owned)
        await advance(1, runtime)
    else:
        tools.gate, tools.started = asyncio.Event(), asyncio.Event()
        running = asyncio.create_task(advance(1, runtime))
        try:
            await asyncio.wait_for(tools.started.wait(), 2)
            business_cancel(owned)
            assert executor.unfinished_files()
            tools.gate.set()
            await running
        finally:
            tools.gate.set()
            await asyncio.gather(running, return_exceptions=True)
    assert owned.connection.execute('SELECT status,cancel_requested FROM actions WHERE id=1').fetchone() == (6, 1)
    assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
    assert len(tools.repairs) == (1 if phase == 'repair' else 0)
    assert load_processing_status(owned, 1).source_probe == source_probe()
    if phase == 'repair':
        # 取消后仍保存已经实际完成的媒体工具及成品事实。
        assert owned.connection.execute('SELECT size_bytes FROM intermediate_files WHERE purpose=4').fetchone() == (23,)
    assert executor.unfinished_files() == ()
    before = (len(tools.probes), len(tools.repairs))
    await advance(1, runtime)
    assert (len(tools.probes), len(tools.repairs)) == before
    paths = tuple(roots.staging / row[0] for row in owned.connection.execute(
        'SELECT relative_path FROM intermediate_files'))
    assert paths and any(path.exists() for path in paths)
    cfg = replace(media_case(flow).config, devices={})
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
    try:
        outcome = await execute_command(deps, None, poll_interval_s=0.001)
        assert outcome.succeeded, outcome
    finally:
        close_runtime(deps)
    assert all(not path.exists() for path in paths)



@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
async def test_real_host_tools_share_executor_through_original_business_saves(pipeline, tmp_path):
    import hashlib
    from camctl.capture.media_facts import saved_output_probe
    owned, roots, source_id = pipeline
    executor = ObservedExecutor()
    tools = ControlledTools(executor)
    tools.content = b'original managed tool bytes'
    flow = await media_flow(pipeline, executor, tools)
    first = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    assert first.phase.value == 'prepared'
    original = load_processing_status(owned, 1)
    assert original.source_probe == source_probe()
    assert executor.unfinished_files() == ()
    done = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    assert done.phase.value == 'succeeded', done
    assert done.output_file.file_id == first.output_file.file_id
    output = roots.staging / done.output_file.relative_path
    content = output.read_bytes()
    assert content == b'original managed tool bytes'
    assert owned.connection.execute('SELECT size_bytes,sha256 FROM intermediate_files WHERE id=?',
        (done.output_file.file_id,)).fetchone() == (len(content), hashlib.sha256(content).hexdigest())
    assert len(saved_output_probe(owned.connection, done.output_file.file_id).streams) == 2
    assert load_processing_status(owned, 1).source_probe == original.source_probe
    assert executor.unfinished_files() == ()


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('fault', ['rollback', 'commit_before', 'commit_after'])
async def test_pending_start_is_saved_before_current_terminal_or_cancel_decision(pipeline, monkeypatch, canceled, fault):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.capture.handlers import capture_handler
    owned = pipeline[0]
    executor = ObservedExecutor()
    tools = ControlledTools(executor)
    flow = await media_flow(pipeline, executor, tools)
    original = CaptureRepository.start_repair_output
    requests = []
    def unknown(repo, command, key, current):
        requests.append((command, key))
        from dataclasses import replace
        from ..bootstrap.test_binding_transactions import _CommitFailure
        if len(requests) == 1:
            connection = (__import__('integration.support.output_planning', fromlist=['storage_fault']).storage_fault(current.connection, 'UPDATE recording_processing')
                if fault == 'rollback' else _CommitFailure(current.connection, fault == 'commit_after'))
            current = replace(current, connection=connection)
        return original(repo, command, key, current)
    monkeypatch.setattr(CaptureRepository, 'start_repair_output', unknown)
    with pytest.raises(ConsistencyError):
        await run_recording_media(flow, 1, 1, pipeline[2], target_extension='osv')
    from dataclasses import replace
    from pathlib import Path
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    owned.connection.close()
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    pipeline = (owned, pipeline[1], pipeline[2])
    flow = await reopen_media(flow, owned)
    try:
        business_cancel(owned)
        if not canceled:
            # 原可靠取消事务先终结动作；本地 START 保存责任仍独立存在。
            await capture_handler('camera_record')(1, (await handler_runtime(pipeline, flow)))
            assert owned.connection.execute('SELECT status FROM actions WHERE id=1').fetchone() == (6,)
        before_tools = list(tools.probes)
        await media_case(flow).resume_saves()
        assert requests == [requests[0], requests[0]]
        assert tools.probes == before_tools and tools.repairs == []
        await capture_handler('camera_record')(1, (await handler_runtime(pipeline, flow)))
        assert owned.connection.execute('SELECT status FROM actions WHERE id=1').fetchone() == (6,)
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
async def test_waiter_cancel_during_prepare_preserves_original_pending_start_diagnostic(pipeline, monkeypatch):
    import asyncio
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    executor = ObservedExecutor()
    tools = ControlledTools(executor)
    tools.gate, tools.started = asyncio.Event(), asyncio.Event()
    flow = await media_flow(pipeline, executor, tools)
    original = CaptureRepository.start_repair_output
    calls = []
    def unknown(repo, command, key, owned):
        calls.append((command, key))
        if len(calls) == 1:
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError('original start save unknown'))
        return original(repo, command, key, owned)
    monkeypatch.setattr(CaptureRepository, 'start_repair_output', unknown)
    running = asyncio.create_task(run_recording_media(flow, 1, 1, pipeline[2], target_extension='osv'))
    try:
        await asyncio.wait_for(tools.started.wait(), 2)
        running.cancel()
        for _ in range(5): await asyncio.sleep(0)
        tools.gate.set()
        with pytest.raises(asyncio.CancelledError) as caught: await running
        assert any('original start save unknown' in n for n in getattr(caught.value, '__notes__', ()))
        assert len(tools.probes) == 1 and tools.repairs == []
        await media_case(flow).resume_saves()
        assert calls == [calls[0], calls[0]]
        assert executor.unfinished_files() == ()
    finally:
        tools.gate.set()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
@pytest.mark.parametrize('result', ['success', 'failed', 'output_probe_failed'])
async def test_waiter_cancel_keeps_obtained_repair_result_without_business_cancel(pipeline, result, monkeypatch):
    import asyncio
    owned, _roots, source_id = pipeline
    executor = ObservedExecutor()
    tools = ControlledTools(executor, result=result)
    flow = await media_flow(pipeline, executor, tools)
    prepared = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    from ..support.capture_media_cases import MediaSaveGate
    saved = MediaSaveGate(monkeypatch,
        'complete_repair_output' if result == 'success' else 'save_repair_result')
    running = asyncio.create_task(run_recording_media(flow, 1, 1, source_id, target_extension='osv'))
    try:
        await asyncio.wait_for(saved.reached.wait(), 2)
        assert executor.unfinished_files() == (tools.probes[0], prepared.output_file.file_id)
        request, _key = saved.requests[0]
        tool = (request.output_probe.tool_diagnostics if result == 'success' else
            request.artifact.tool_diagnostics if result == 'failed' else request.failure_probe.tool_diagnostics)
        assert tool.outcome.error is None
        assert tool.outcome.exit.exit_code == (0 if result == 'success' else 1)
        running.cancel()
        for _ in range(5): await asyncio.sleep(0)
        assert not running.done()
        saved.release.set()
        with pytest.raises(asyncio.CancelledError): await running
        from ..support.capture_media_cases import write_tool_evidence
        write_tool_evidence(pipeline[1].staging.parent, executor)
        assert load_processing_status(owned, 1).repair_state == (5 if result == 'success' else 6)
        assert owned.connection.execute('SELECT cancel_requested FROM actions WHERE id=1').fetchone() == (0,)
        assert tools.repairs == [prepared.output_file.file_id]
        assert executor.unfinished_files() == ()
        counts = len(tools.probes), len(tools.repairs)
        await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
        assert counts == (len(tools.probes), len(tools.repairs))
    finally:
        saved.release.set()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
@pytest.mark.parametrize('stage', ['probe', 'repair'])
async def test_actual_inflight_tool_stop_preserves_cancelled_failure_after_exit_zero(pipeline, stage):
    import asyncio
    import json
    from camctl.host_files.media import MediaArtifact
    from ..support.capture_media_cases import write_tool_evidence
    owned, roots, source_id = pipeline
    executor = ObservedExecutor()
    tools = ControlledTools(executor)
    flow = await media_flow(pipeline, executor, tools)
    if stage == 'repair':
        prepared = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
        assert prepared.phase.value == 'prepared'
    tools.gate, tools.started = asyncio.Event(), asyncio.Event()
    running = asyncio.create_task(run_recording_media(flow, 1, 1, source_id, target_extension='osv'))
    try:
        await asyncio.wait_for(tools.started.wait(), 2)
        running.cancel()
        async def stopped():
            while not (tools.directory / 'stop-observed').exists():
                await asyncio.sleep(.005)
        await asyncio.wait_for(stopped(), 2)
        assert executor.unfinished_files()
        tools.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        actual = [result.value for _kind, result in executor.results
            if isinstance(result.value, MediaProbe if stage == 'probe' else MediaArtifact)][-1]
        raw = actual.tool_diagnostics.outcome
        assert raw.error == 'cancelled' and raw.exit.exit_code == 0
        if stage == 'probe':
            document = json.loads(raw.output)
            assert document['format']['duration'] == '75'
            assert len(document['streams']) == 2
            assert actual.duration_s is None and actual.streams == ()
            assert actual.error.startswith('tool_failed: cancelled;')
            assert load_processing_status(owned, 1).source_probe.error.startswith('tool_failed: cancelled;')
            failed = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
            assert failed.phase.value == 'failed_saved'
            assert tools.repairs == []
        else:
            assert actual.tool_error.startswith('tool_failed: cancelled;')
            assert actual.observation.size_bytes == len(tools.content)
            assert actual.observation.path.read_bytes() == tools.content
            assert len(tools.repairs) == 1
        assert load_processing_status(owned, 1).repair_state == 6
        assert owned.connection.execute('SELECT cancel_requested FROM actions WHERE id=1').fetchone() == (0,)
        assert executor.unfinished_files() == ()
        before = (tools.probes, tools.repairs)
        await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
        assert (tools.probes, tools.repairs) == before
        write_tool_evidence(roots.staging.parent, executor)
    finally:
        tools.gate.set()
        await asyncio.gather(running, return_exceptions=True)
