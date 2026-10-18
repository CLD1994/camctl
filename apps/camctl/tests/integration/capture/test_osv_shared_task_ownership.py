"""OSV 准备结束后才能为原输入和唯一输出取得完整修复资格。"""
from decimal import Decimal

import pytest

from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, MediaFlow, load_processing_status, run_recording_media
from camctl.host_files.media import MediaProbe, MediaStream
from camctl.host_files.tasks import AsyncFileTask, FileTaskExecutor, FileTaskId
from camctl.session.supervision import Supervisor

from .media_retry_fixtures import media_pipeline as pipeline
from .test_input_copy import _CONTENT
from .test_media_flow import ReadDriverDouble, _Digest

pytestmark = pytest.mark.asyncio


def source_probe():
    return MediaProbe(Decimal('75'), None, (
        MediaStream(0, 'video', 'h264', 64, 64, Decimal('75'), False),
        MediaStream(1, 'video', 'h264', 64, 64, Decimal('75'), False),
    ), 'mov,mp4,m4a,3gp,3g2,mj2')


class _Owner:
    async def take_over(self, task):
        await task.pending.wait()


class Tools:
    def __init__(self, executor):
        self.executor, self.probes, self.repairs = executor, [], []

    async def probe(self, input):
        async def body(_control):
            self.probes.append(input.file_id)
            return source_probe()
        return await self.executor.run_async_file_task(AsyncFileTask(
            FileTaskId('source-probe'), (input.file_id,), 'probe', '原输入事实', body), _Owner())

    async def repair(self, input, output, **kwargs):
        self.repairs.append(output.file_id)
        raise AssertionError('准备阶段不得启动修复')


def media_flow(pipeline, executor, tools):
    owned, roots, _source = pipeline
    return MediaFlow(owned=owned, roots=roots,
        sessions=DriverReadSessions(owned, ReadDriverDouble(_CONTENT), ticket=None),
        tools=tools, policy=MediaPolicy(Decimal('2')),
        occurred_at=lambda: 1_750_000_100_000_000,
        digest_supported=True, digest=_Digest(_CONTENT), file_executor=executor)


@pytest.mark.parametrize('pipeline', ['osv'], indirect=True)
async def test_pending_osv_returns_prepared_after_releasing_input_owner(pipeline):
    owned, _roots, source_id = pipeline
    executor = FileTaskExecutor(Supervisor())
    tools = Tools(executor)
    step = await run_recording_media(media_flow(pipeline, executor, tools), 1, 1, source_id,
        target_extension='osv')
    assert step.phase.value == 'prepared'
    status = load_processing_status(owned, 1)
    assert status.repair_state == 4 and status.source_probe == source_probe()
    assert status.repair_output_file_id == step.output_file.file_id
    assert owned.connection.execute('SELECT COUNT(*) FROM intermediate_files WHERE purpose=4').fetchone() == (1,)
    assert len(tools.probes) == 1 and tools.repairs == []
    assert executor.unfinished_files() == ()


# 观察器只记录实际受理和结束；所有裁决仍由原执行器执行。
class ObservedExecutor(FileTaskExecutor):
    def __init__(self):
        super().__init__(Supervisor())
        self.events = []

    def _admit(self, task):
        state = super()._admit(task)
        self.events.append(('admit', task.stage, task.file_ids, task.task_id))
        return state

    def _release_identity(self, state):
        super()._release_identity(state)
        self.events.append(("delivered", state.task.stage, self.unfinished_files(), state.task.task_id))

    def _finish(self, state, result):
        super()._finish(state, result)
        self.events.append(('finish', state.task.stage, self.unfinished_files(), state.task.task_id))


class ControlledTools(Tools):
    def __init__(self, executor, *, probe=None, result='success', probe_ran=True):
        super().__init__(executor)
        self.source = source_probe() if probe is None else probe
        self.result, self.probe_ran = result, probe_ran
        self.gate = None
        self.started = None
        self.controls = []

    async def probe(self, input):
        from dataclasses import replace
        from camctl.host_files.models import FilePurpose
        from camctl.host_files.tasks import FileTaskResult
        async def body(control):
            self.controls.append(control)
            self.probes.append(input.file_id)
            if self.gate is not None and input.purpose is FilePurpose.RECORDING_INPUT:
                self.started.set()
                await self.gate.wait()
            if input.purpose is FilePurpose.REPAIR_OUTPUT:
                if self.result == 'output_probe_failed':
                    return MediaProbe(None, 'tool_failed: actual output probe')
                return replace(self.source, duration_s=Decimal('60'), streams=tuple(
                    replace(s, duration_s=Decimal('60')) for s in self.source.streams))
            return self.source
        if not self.probe_ran:
            return FileTaskResult(FileTaskId('not-run'), ran=False)
        return await self.executor.run_async_file_task(AsyncFileTask(
            FileTaskId('actual-probe'), (input.file_id,), 'probe', '原媒体事实', body), _Owner())

    async def repair(self, input, output, **kwargs):
        import hashlib
        from pathlib import Path
        from camctl.host_files.io import HashResult
        from camctl.host_files.media import MediaArtifact
        from camctl.host_files.models import FileObservation, FileObservationKind
        async def body(control):
            self.controls.append(control)
            self.repairs.append(output.file_id)
            if self.gate is not None:
                self.started.set()
                await self.gate.wait()
            path = Path(output.root) / output.relative_path
            if self.result == 'failed':
                return MediaArtifact('tool_failed: actual repair result',
                    FileObservation(FileObservationKind.MISSING, path))
            content = b'original repaired bytes'
            path.write_bytes(content)
            return MediaArtifact(None, FileObservation(FileObservationKind.VALID_OBJECT,
                path, True, size_bytes=len(content)),
                HashResult(hashlib.sha256(content).hexdigest(), len(content), None),
                postprocessing_stopped=True)
        return await self.executor.run_async_file_task(AsyncFileTask(
            FileTaskId('actual-repair'), (input.file_id, output.file_id), 'repair', '原成品事实', body), _Owner())


@pytest.mark.parametrize('pipeline', ['osv-timing', 'osv'], indirect=True)
@pytest.mark.parametrize('result', ['success', 'failed', 'output_probe_failed'])
async def test_two_stages_reuse_saved_probe_and_output_with_complete_ownership(pipeline, result):
    owned, _roots, source_id = pipeline
    executor = ObservedExecutor()
    tools = ControlledTools(executor, result=result)
    flow = media_flow(pipeline, executor, tools)
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
    if fact == 'tool_failed': probe = MediaProbe(None, 'tool_failed: actual source probe')
    if fact == 'missing_duration': probe = replace(probe, duration_s=None)
    if fact == 'missing_video': probe = replace(probe, streams=probe.streams[:1])
    executor = ObservedExecutor()
    tools = ControlledTools(executor, probe=probe, probe_ran=fact != 'not_run')
    flow = media_flow(pipeline, executor, tools)
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
    from camctl.capture.media_flow import resume_media_saves
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from ..bootstrap.test_binding_transactions import _CommitFailure
    from ..operations.test_result_reuse import _FaultConnection
    owned, roots, source_id = pipeline
    executor = ObservedExecutor()
    tools = ControlledTools(executor, result=result)
    flow = media_flow(pipeline, executor, tools)
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
            connection = (_FaultConnection(current.connection, 'UPDATE recording_processing')
                if fault == 'rollback' else _CommitFailure(current.connection, fault == 'commit_after'))
            current = replace(current, connection=connection)
        outcome = original(repo, command, key, current)
        outcomes.append(outcome)
        return outcome
    monkeypatch.setattr(CaptureRepository, stage, save)
    with pytest.raises(ConsistencyError):
        await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    assert len(requests) == 1 and len(flow.pending_media_results) == 1
    assert outcomes[0].kind is (DbOutcomeKind.ROLLED_BACK if fault == 'rollback' else DbOutcomeKind.UNKNOWN)
    held, = flow.pending_media_results.values()
    assert (held.command, held.key) == requests[0]
    assert len(held.file_ids) == (1 if stage == "start_repair_output" else 2)
    probes, repairs = list(tools.probes), list(tools.repairs)
    path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    owned.connection.close()
    reopened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        # 保存恢复入口先完成原申请；当前时刻不会参与重算原命令。
        await resume_media_saves(reopened, flow.pending_media_results, executor)
        assert requests == [requests[0], requests[0]]
        assert outcomes[1].kind is DbOutcomeKind.COMPLETED, outcomes[1].error
        if fault == 'commit_after': assert outcomes[1].value.disposition.value == 'already'
        assert tools.probes == probes and tools.repairs == repairs
        assert not flow.pending_media_results
        status = load_processing_status(reopened, 1)
        assert status.source_probe == source_probe()
        assert reopened.connection.execute('SELECT COUNT(*) FROM intermediate_files WHERE purpose=4').fetchone() == (1,)
        if stage == 'start_repair_output':
            flow = replace(flow, owned=reopened, sessions=DriverReadSessions(reopened,
                ReadDriverDouble(_CONTENT), ticket=None), occurred_at=lambda: 1_750_000_200_000_000)
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
    flow = media_flow(pipeline, executor, tools)
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


class GatedExecutor(ObservedExecutor):
    def __init__(self, phase):
        import asyncio
        super().__init__()
        self.phase, self.reached, self.release = phase, asyncio.Event(), asyncio.Event()

    async def _execute_async(self, state):
        if state.task.stage != 'media_repair_prepare':
            return await super()._execute_async(state)
        if self.phase == 'queued':
            self.reached.set()
            await self.release.wait()
        result = await super()._execute_async(state)
        if self.phase == 'finished':
            self.reached.set()
            await self.release.wait()
        return result


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
@pytest.mark.parametrize('phase', ['queued', 'probe', 'finished'])
async def test_waiter_cancel_keeps_started_prepare_result_and_never_dispatches_repair(pipeline, phase):
    import asyncio
    executor = GatedExecutor(phase)
    tools = ControlledTools(executor)
    if phase == 'probe':
        tools.gate, tools.started = asyncio.Event(), asyncio.Event()
        reached, release = tools.started, tools.gate
    else:
        reached, release = executor.reached, executor.release
    flow = media_flow(pipeline, executor, tools)
    running = asyncio.create_task(run_recording_media(flow, 1, 1, pipeline[2], target_extension='osv'))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        running.cancel()
        # 让取消到达原等待边界；不以 sleep 时长制造时序。
        for _ in range(5): await asyncio.sleep(0)
        if phase == 'probe':
            assert not running.done() and executor.unfinished_files()
        release.set()
        with pytest.raises(asyncio.CancelledError): await running
        status = load_processing_status(pipeline[0], 1)
        assert status.repair_state == (3 if phase == 'queued' else 4)
        assert status.source_probe == (None if phase == 'queued' else source_probe())
        assert len(tools.probes) == (0 if phase == 'queued' else 1)
        assert tools.repairs == [] and executor.unfinished_files() == ()
        assert pipeline[0].connection.execute('SELECT cancel_requested FROM actions WHERE id=1').fetchone() == (0,)
        settled = await executor.drain_required()
        assert not settled.failed
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)


def handler_runtime(pipeline, flow):
    from dataclasses import replace
    from .media_retry_fixtures import _EVIDENCE, original_recording_entry
    from .test_capture_contract import ResultsDouble, _runtime
    entry = replace(original_recording_entry(), original_name='original.OSV',
        locator={'path': '/DCIM/original.OSV'}, format_id='osv')
    runtime = _runtime(pipeline[0], results=ResultsDouble({1: (entry,)}), listing_cache={})
    runtime.evidence = _EVIDENCE
    runtime.media, runtime.file_executor = flow, flow.file_executor
    return runtime


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
    flow = media_flow(pipeline, executor, tools)
    runtime = handler_runtime(pipeline, flow)
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
    from camctl.bootstrap.work_file_assembly import WorkFileRuntime
    from camctl.outputs.work_files import WorkFileLimits
    owned, roots, _source = pipeline
    work = WorkFileRuntime(staging=roots.staging, limits=WorkFileLimits(100, 100))
    executor = work.executor
    tools = ControlledTools(executor)
    flow = media_flow(pipeline, executor, tools)
    runtime = handler_runtime(pipeline, flow)
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
    file_ids = tuple(r[0] for r in owned.connection.execute('SELECT id FROM intermediate_files'))
    outcome = await work.first_cleanup(file_ids, owned=owned, occurred_at=1_750_000_200_000_000)
    assert outcome.complete
    assert all(not (roots.staging / r[0]).exists() for r in owned.connection.execute('SELECT relative_path FROM intermediate_files'))
    work.stop_new_work()
    assert not (await work.file_supervisor.drain_required()).failed


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
async def test_real_host_tools_share_executor_through_original_business_saves(pipeline, tmp_path):
    import hashlib
    from camctl.bootstrap.capture_assembly import HostMediaTools
    from camctl.capture.media_facts import saved_output_probe
    from camctl.host_files.media import ProbeRequest, RepairRequest
    from ..host_files.test_media import _tool
    owned, roots, source_id = pipeline
    executor = ObservedExecutor()
    probe = _tool(tmp_path, 'shared-probe', '''import json, sys
seconds = '60' if '/derived/' in sys.argv[-1] else '75'
print(json.dumps({'format': {'duration': seconds, 'format_name': 'mov,mp4,m4a,3gp,3g2,mj2'},
    'streams': [{'index': i, 'codec_type': 'video', 'codec_name': 'h264',
        'width': 64, 'height': 64, 'duration': seconds, 'disposition': {'attached_pic': 0}}
        for i in range(2)]}))
''')
    repair = _tool(tmp_path, 'shared-repair', '''import pathlib, sys
assert sys.argv.count('-map') == 2
assert sys.argv[sys.argv.index('-f') + 1] == 'mp4'
pathlib.Path(sys.argv[-1]).write_bytes(b'original managed tool bytes')
''')
    tools = HostMediaTools(roots, executor, _Owner(),
        probe_request=ProbeRequest(ffprobe=probe), repair_request=RepairRequest(ffmpeg=repair))
    flow = media_flow(pipeline, executor, tools)
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
    from camctl.capture.media_flow import resume_media_saves
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.capture.handlers import capture_handler
    owned = pipeline[0]
    executor = ObservedExecutor()
    tools = ControlledTools(executor)
    flow = media_flow(pipeline, executor, tools)
    original = CaptureRepository.start_repair_output
    requests = []
    def unknown(repo, command, key, current):
        requests.append((command, key))
        from dataclasses import replace
        from ..bootstrap.test_binding_transactions import _CommitFailure
        from ..operations.test_result_reuse import _FaultConnection
        if len(requests) == 1:
            connection = (_FaultConnection(current.connection, 'UPDATE recording_processing')
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
    flow = replace(flow, owned=owned,
        sessions=DriverReadSessions(owned, ReadDriverDouble(_CONTENT), ticket=None))
    try:
        business_cancel(owned)
        if not canceled:
            # 原可靠取消事务先终结动作；本地 START 保存责任仍独立存在。
            await capture_handler('camera_record')(1, handler_runtime(pipeline, flow))
            assert owned.connection.execute('SELECT status FROM actions WHERE id=1').fetchone() == (6,)
        before_tools = list(tools.probes)
        await resume_media_saves(owned, flow.pending_media_results, executor)
        assert requests == [requests[0], requests[0]]
        assert not flow.pending_media_results and tools.probes == before_tools and tools.repairs == []
        await capture_handler('camera_record')(1, handler_runtime(pipeline, flow))
        assert owned.connection.execute('SELECT status FROM actions WHERE id=1').fetchone() == (6,)
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
async def test_waiter_cancel_during_prepare_preserves_original_pending_start_diagnostic(pipeline, monkeypatch):
    import asyncio
    from camctl.capture.media_flow import resume_media_saves
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    executor = ObservedExecutor()
    tools = ControlledTools(executor)
    tools.gate, tools.started = asyncio.Event(), asyncio.Event()
    flow = media_flow(pipeline, executor, tools)
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
        assert len(flow.pending_media_results) == 1
        assert len(tools.probes) == 1 and tools.repairs == []
        await resume_media_saves(pipeline[0], flow.pending_media_results, executor)
        assert calls == [calls[0], calls[0]]
        assert executor.unfinished_files() == ()
    finally:
        tools.gate.set()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize('pipeline', ['osv-timing'], indirect=True)
@pytest.mark.parametrize('result', ['success', 'failed', 'output_probe_failed'])
async def test_waiter_cancel_in_full_repair_keeps_actual_result_without_business_cancel(pipeline, result):
    import asyncio
    owned, _roots, source_id = pipeline
    executor = ObservedExecutor()
    tools = ControlledTools(executor, result=result)
    flow = media_flow(pipeline, executor, tools)
    prepared = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
    tools.gate, tools.started = asyncio.Event(), asyncio.Event()
    running = asyncio.create_task(run_recording_media(flow, 1, 1, source_id, target_extension='osv'))
    try:
        await asyncio.wait_for(tools.started.wait(), 2)
        assert executor.unfinished_files() == (tools.probes[0], prepared.output_file.file_id)
        running.cancel()
        for _ in range(5): await asyncio.sleep(0)
        assert not running.done()
        tools.gate.set()
        with pytest.raises(asyncio.CancelledError): await running
        assert load_processing_status(owned, 1).repair_state == (5 if result == 'success' else 6)
        assert owned.connection.execute('SELECT cancel_requested FROM actions WHERE id=1').fetchone() == (0,)
        assert tools.repairs == [prepared.output_file.file_id]
        assert executor.unfinished_files() == ()
        counts = len(tools.probes), len(tools.repairs)
        await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
        assert counts == (len(tools.probes), len(tools.repairs))
    finally:
        tools.gate.set()
        await asyncio.gather(running, return_exceptions=True)
