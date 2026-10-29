"""媒体消费者的正式工厂、实际工具脚本和数据库重开支持。"""

import asyncio
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest_asyncio

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.host_files.media import ProbeRequest, RepairRequest
from camctl.host_files.tasks import FileTaskExecutor
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.session.supervision import Supervisor

from .capture import capture_database, restart_capture_database

_CASES = ContextVar('capture_media_test_cases', default=None)
_NOW = 1_750_000_000_000_000
CONTENT = b'0123456789'


@pytest_asyncio.fixture(autouse=True)
async def media_cases(capture_resources):
    cases = []
    token = _CASES.set(cases)
    try:
        yield cases
    finally:
        errors = []
        for case in reversed(cases):
            try:
                await case.close()
            except BaseException as error:
                errors.append(error)
        _CASES.reset(token)
        if errors:
            raise BaseExceptionGroup('媒体消费者实际资源收场失败', errors)


def probe_json(probe):
    """测试工具只提供 ffprobe 的正式输出格式。"""
    result = {'format': {}, 'streams': []}
    if probe.duration_s is not None:
        result['format']['duration'] = str(probe.duration_s)
    if probe.format_name is not None:
        result['format']['format_name'] = probe.format_name
    for stream in probe.streams:
        value = {key: getattr(stream, key) for key in (
            'index', 'codec_type', 'codec_name', 'width', 'height') if getattr(stream, key) is not None}
        if stream.duration_s is not None:
            value['duration'] = str(stream.duration_s)
        if stream.attached_pic is not None:
            value['disposition'] = {'attached_pic': int(stream.attached_pic)}
        if stream.frame_count is not None:
            value['nb_read_frames'] = str(stream.frame_count)
        if stream.fps is not None:
            ratio = stream.fps.as_integer_ratio()
            value['avg_frame_rate'] = f'{ratio[0]}/{ratio[1]}'
        result['streams'].append(value)
    if probe.error is not None and probe.error.startswith('no_video_stream'):
        result['format']['duration'] = '0'
    return result


class ScriptTools:
    """通过工具路径、JSON 输出、返回码及文件门闩控制真实子进程。"""

    def __init__(self, source, *, result='success', content=b'original repaired bytes'):
        self.source, self.result, self.content = source, result, content
        self.directory = None
        self._gate = self._started = None
        self.watchers = []

    def configure(self, directory):
        if self.directory is not None:
            return ProbeRequest(ffprobe=str(self.directory / 'probe')), RepairRequest(ffmpeg=str(self.directory / 'repair'))
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = self.directory / 'calls.jsonl'
        self.settings = self.directory / 'settings.json'
        self.refresh()
        common = f'''import json, pathlib, signal, sys, time
root = pathlib.Path({str(self.directory)!r})
settings = json.loads((root / 'settings.json').read_text())
path = pathlib.Path(sys.argv[-1])
identity = int(path.stem)
stage = 'probe' if pathlib.Path(sys.argv[0]).name == 'probe' else 'repair'
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps([stage, identity, str(path)]) + '\\n')
if settings.get('gate') and (stage == 'repair' or '/recording-inputs/' in str(path)):
    def stop_observed(*_):
        (root / 'stop-observed').touch()
    signal.signal(signal.SIGTERM, stop_observed)
    (root / 'started').touch()
    while not (root / 'release').exists():
        time.sleep(.005)
'''
        probe = common + '''output = '/derived/' in str(path)
if settings['result'] == 'output_probe_failed' and output:
    sys.stderr.write('actual output probe error')
    sys.exit(1)
if settings['source_error'] and not settings['source_error'].startswith(('no_video_stream', 'missing_duration')):
    sys.stderr.write('actual source probe error')
    sys.exit(1)
print(json.dumps(settings['output'] if output else settings['source']))
'''
        repair = common + '''if settings['result'] == 'failed':
    sys.stderr.write('actual repair result')
    sys.exit(1)
path.write_bytes(bytes.fromhex(settings['content']))
'''
        for name, body in (('probe', probe), ('repair', repair)):
            path = self.directory / name
            path.write_text('#!' + sys.executable + '\n' + body)
            path.chmod(0o755)
        return ProbeRequest(ffprobe=str(self.directory / 'probe')), RepairRequest(ffmpeg=str(self.directory / 'repair'))

    def refresh(self):
        if self.directory is None:
            return
        output = replace(self.source, duration_s=Decimal(60), error=None, streams=tuple(
            replace(stream, duration_s=Decimal(60)) for stream in self.source.streams
            if stream.attached_pic is not True))
        self.settings.write_text(json.dumps({
            'source': probe_json(self.source), 'source_error': self.source.error,
            'output': probe_json(output), 'result': self.result, 'content': self.content.hex(),
            'gate': self._gate is not None}))

    def entries(self):
        if self.directory is None or not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    @property
    def probes(self):
        return [entry[1] for entry in self.entries() if entry[0] == 'probe']

    @property
    def repairs(self):
        return [entry[1] for entry in self.entries() if entry[0] == 'repair']

    @property
    def calls(self):
        return [entry[0] for entry in self.entries()]

    @property
    def gate(self):
        return self._gate

    @gate.setter
    def gate(self, value):
        self._gate = value
        if self.directory is not None:
            for name in ('started', 'release', 'stop-observed'):
                (self.directory / name).unlink(missing_ok=True)
            self.refresh()

    @property
    def started(self):
        return self._started

    @started.setter
    def started(self, value):
        self._started = value
        async def watch():
            while not (self.directory / 'started').exists():
                await asyncio.sleep(.005)
            value.set()
            await self._gate.wait()
            (self.directory / 'release').touch()
        self.watchers.append(asyncio.create_task(watch()))

    async def close(self):
        if self._gate is not None:
            self._gate.set()
        if self.directory is not None:
            (self.directory / 'release').touch()
        for task in self.watchers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.watchers, return_exceptions=True)


class Results:
    """正式 ResultFilesPort 的可控输入；不后改拍摄运行时。"""
    def __init__(self, delegate):
        self.delegate = delegate

    async def list_round(self, *args, **kwargs):
        return await self.delegate.list_round(*args, **kwargs)

    async def list_page(self, *args, **kwargs):
        return await self.delegate.list_page(*args, **kwargs)

    def close_scan(self, *args, **kwargs):
        method = getattr(self.delegate, 'close_scan', None)
        if method is not None:
            method(*args, **kwargs)


class MediaCase:
    def __init__(self, owned, roots, source_id, tools, *, executor=None, read=True, margin=Decimal(2), results=None):
        from ..bootstrap.test_media_assembly import _SessionDriver, _config
        from ..capture.media_retry_fixtures import _EVIDENCE, original_recording_entry
        from ..capture.test_capture_contract import ResultsDouble
        self.owned, self.roots, self.source_id, self.tools = owned, roots, source_id, tools
        self.path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        self.supervisor = Supervisor()
        self.executor = executor or FileTaskExecutor(self.supervisor)
        self.config = _config(self.path.parent)
        self.driver = _SessionDriver(CONTENT)
        devices = {identity: {name: dict(value) if isinstance(value, Mapping) else value
            for name, value in declaration.items()}
            for identity, declaration in self.config.devices.items()}
        devices['cam-1'].setdefault('recording', {})['repair_margin_s'] = margin
        devices['cam-1'].setdefault('copy', {})['retry_interval_s'] = Decimal(0)
        if results is None:
            name, locator = owned.connection.execute(
                'SELECT original_name,locator_json FROM device_files WHERE id=?', (source_id,)).fetchone()
            format_id = Path(name).suffix.lstrip('.').lower()
            entry = replace(original_recording_entry(), original_name=name,
                locator=json.loads(locator), format_id=format_id)
            results = ResultsDouble({1: (entry,)})
        self.results = Results(results)
        registry = DriverRegistry((DriverEntry('camctl-adb', self.driver, DriverDeclaration(
            control_supported=True, stop_supported=True, query_supported=False,
            result_supported=False, read_supported=read, digest_supported=read, delete_supported=False),
            _EVIDENCE, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
        requests = tools.configure(self.path.parent / f'media-tools-{len(_CASES.get())}') if tools is not None else (None, None)
        self.recovery_inputs = {}
        self.wall = [_NOW + 100_000_000]
        self.mono = [7_000_000_000]
        self.factory = session_capture_assembly(devices=devices, drivers=registry,
            results=self.results, staging=roots.staging, wait_config=lambda _: None,
            wall_us=lambda: self.wall[0], monotonic_ns=lambda: self.mono[0],
            probe_request=requests[0], repair_request=requests[1], file_executor=self.executor,
            pending_media_results=self.recovery_inputs)
        self.runtime = None
        _CASES.get().append(self)

    async def start(self):
        self.database = await capture_database(self.owned)
        self.runtime = await self.factory(self.database, 'cam-1')
        return self

    async def reopen(self, owned):
        settled = await self.executor.drain_required()
        assert not settled.failed, settled.failed
        self.database = await restart_capture_database(owned)
        self.owned = owned
        self.runtime = await self.factory(self.database, 'cam-1')
        return self.runtime.media

    async def resume_saves(self):
        from camctl.capture.media_flow import resume_media_saves
        await resume_media_saves(self.database, self.recovery_inputs, self.executor)

    async def close(self):
        if self.tools is not None:
            await self.tools.close()
        settled = await self.executor.drain_required()
        errors = [failure.exception or RuntimeError(failure.error) for failure in settled.failed]
        if errors:
            raise BaseExceptionGroup('媒体文件执行器未完整收场', errors)


async def assembled_media(pipeline, executor, tools, *, margin=Decimal(2)):
    case = await MediaCase(*pipeline[:2], pipeline[2], tools, executor=executor, margin=margin).start()
    assert case.runtime.media is not None
    return case.runtime.media


def media_case(flow):
    return next(case for case in _CASES.get() if case.runtime.media is flow)


async def reopen_media(flow, owned):
    return await media_case(flow).reopen(owned)


def handler_runtime(flow):
    return media_case(flow).runtime


_ENVIRONMENTS = ContextVar('capture_media_recording_environments', default=None)


async def recording_environment(tmp_path, *, check_decision, check_basis):
    """真实受理、START 和 STOP 后，在公开活动收场保存边界交付准备结果。"""
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
    from camctl.capture.processing import CheckBasis, CheckDecisionChoice, CheckDecisionSave, CheckReason
    from camctl.host_files.models import BoundDirectories
    from camctl.persistence.initialization import InitOutcome, initialize_state
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.scheduling import ObserveWindowRequest, SchedulingRepository, StartActionRequest
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from ..bootstrap.test_media_assembly import _config
    from ..capture.media_retry_fixtures import _Catalog
    from ..capture.test_capture_contract import ResultsDouble

    cfg = _config(tmp_path)
    path = Path(cfg.paths.state_db)
    assert initialize_state(cfg, path).outcome is InitOutcome.CREATED
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    database = await capture_database(owned)
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    accepted = await accept_input(ParsedInput('recording-media.json', {
        'request_id': '1', 'created_at': instant, 'name': '录像媒体链',
        'actions': [{'name': '录像', 'type': 'camera_record', 'device_id': 'cam-1',
            'scheduled_at': instant, 'params': {'type': 'video'}, 'policy': {'max_delay_ms': 5000}}]}),
        AcceptanceContext(CommandMode.RUN, _Catalog(), AcceptanceRepository(),
            clock=SimpleNamespace(utc_micros=lambda: _NOW)), new_operation_key(), database)
    assert accepted.plan_id is not None, accepted
    scheduling = __import__('camctl.persistence.repositories.scheduling', fromlist=['SchedulingRepository']).SchedulingRepository()
    for operation, request in (
        (scheduling.observe_window, ObserveWindowRequest(1, _NOW, _NOW)),
        (scheduling.start_action, StartActionRequest(1, _NOW, _NOW)),
    ):
        receipt = operation(request, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    roots = BoundDirectories(staging=Path(cfg.paths.staging))
    case = await MediaCase(owned, roots, None, None, read=False, results=ResultsDouble({})).start()
    case.wall[0] = _NOW
    case.mono[0] = 7_000_000_000
    await capture_handler('camera_record')(1, case.runtime)
    if check_decision == 3:
        # 新会话没有前会话连续控制锚点，媒体检查基于真实恢复分类。
        case = await MediaCase(owned, roots, None, None, read=False, results=ResultsDouble({})).start()
    case.wall[0] = _NOW + 60_000_000
    case.mono[0] = 67_000_000_000
    actual = CaptureRepository.conclude_activity
    class ActivitySaved(Exception):
        pass
    def conclude(repository, request, key, current):
        receipt = actual(repository, request, key, current)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return replace(receipt, handoff_error=ActivitySaved())
    with patch.object(CaptureRepository, 'conclude_activity', conclude):
        try:
            await capture_handler('camera_record')(1, case.runtime)
        except ActivitySaved:
            pass
        else:
            raise AssertionError('准备须实际停止并保存活动收场')
    if check_decision != 1:
        raw = json.loads(check_basis)
        basis = CheckBasis(CheckReason(raw['reason']), raw['target_duration_ms'],
            control_elapsed_ns=raw.get('control_elapsed_ns'))
        receipt = CaptureRepository().save_check_decision(CheckDecisionSave(
            1, CheckDecisionChoice(check_decision), basis, case.wall[0]), new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    if _ENVIRONMENTS.get() is None:
        _ENVIRONMENTS.set({})
    _ENVIRONMENTS.get()[str(path)] = (owned, roots)
    return owned, roots.staging


async def recording_runtime(owned, files, flow=None):
    from ..capture.test_capture_contract import ResultsDouble
    if flow is not None:
        case = media_case(flow)
        case.results.delegate = ResultsDouble(files)
        return case.runtime
    path = owned.connection.execute('PRAGMA database_list').fetchone()[2]
    owned, roots = _ENVIRONMENTS.get()[path]
    return (await MediaCase(owned, roots, None, None, read=False, results=ResultsDouble(files)).start()).runtime


async def recording_media(owned, tools, staging, *, margin=Decimal(2)):
    from camctl.host_files.models import BoundDirectories
    from ..capture.test_capture_contract import ResultsDouble
    case = await MediaCase(owned, BoundDirectories(staging=staging), None, tools,
        margin=margin, results=ResultsDouble({})).start()
    return case.runtime.media


def save_source_entry(owned, entry, *, processing_id=1):
    """媒体存储专项按公开文件观察、归属、完成和输入命令准备前置。"""
    from camctl.capture.files import FileObservationSave, FilePresenceSave, OwnershipSave, FileCompletionSave
    from camctl.capture.processing import SourceFileSave
    repository = CaptureRepository()
    saved = repository.save_file_observation(FileObservationSave(
        1, entry.identity, entry.locator, _NOW, entry.original_name, entry.media_type),
        new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    source_id = saved.value.file_id
    for method, request in (
        (repository.save_file_presence, FilePresenceSave(source_id, 2, _NOW, locator=entry.locator)),
        (repository.save_file_ownership, OwnershipSave(source_id, 1, 1, 2, {'task': entry.identity}, _NOW)),
        (repository.save_file_completion, FileCompletionSave(source_id, 3, _NOW, basis=1,
            observation=entry.evidence, size_bytes=entry.size_bytes)),
        (repository.save_source_file, SourceFileSave(processing_id, source_id, _NOW)),
    ):
        outcome = method(request, new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return source_id


async def cold_media(flow, owned):
    """原worker实际结束后建立新工厂，只使用耐久媒体事实继续。"""
    old = media_case(flow)
    settled = await old.executor.drain_required()
    assert not settled.failed, settled.failed
    await restart_capture_database(owned)
    current = await MediaCase(owned, old.roots, old.source_id, old.tools,
        results=old.results.delegate).start()
    return current.runtime.media


def processing_success_event(owned, command):
    """从公开成功仓储的真实事务取得 H1 独立事件输入。"""
    from .output_planning import capture_operation, event_case
    proposal = capture_operation(owned, lambda current:
        CaptureRepository().complete_repair_output(command, new_operation_key(), current))
    event = next(event for event in proposal.events if any(
        row.table == 'recording_processing' and row.after.values.get('repair_state') == 5
        for row in event.rows))
    return event_case(proposal, event.event_type)


def capture_finish_request(owned, output_id):
    from camctl.outputs.catalog import FileReference, OutputCatalogFacts, OutputDraft, OutputKind
    from camctl.persistence.repositories.capture import FinishCapture
    source_id, = owned.connection.execute(
        'SELECT source_device_file_id FROM recording_processing WHERE id=1').fetchone()
    return FinishCapture(1, (
        OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=source_id), True),
        OutputDraft(OutputKind.REPAIRED, FileReference(intermediate_file_id=output_id), True,
                    original_batch_file_id=source_id),
    ), OutputCatalogFacts(1, True), _NOW + 300)


def promotion_event(owned, output_id):
    from .output_planning import capture_operation, event_case
    request = capture_finish_request(owned, output_id)
    proposal = capture_operation(owned, lambda current:
        CaptureRepository().finish_capture(request, new_operation_key(), current))
    event = next(event for event in proposal.events if any(
        row.table == 'outputs' and row.after.values.get('intermediate_file_id') == output_id
        for row in event.rows))
    from copy import deepcopy
    from .output_planning import apply_values
    state = deepcopy(proposal.state_rows)
    index = proposal.events.index(event)
    for previous in proposal.events[:index]:
        apply_values(state, previous)
    following = deepcopy(state)
    for current in proposal.events[index:]:
        apply_values(following, current)
    return event, replace(proposal, state_rows=state, transaction_rows=following)


def validate_media_event(owned, event, proposal, *, scope='valid'):
    """H1 静态校验使用实际仓储事件、明确当前事实及真实 PlanReadScope。"""
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.reads import PlanReadScope
    from camctl.history.validators import EventContext, validate_event
    from camctl.persistence.event_reads import SqlEventReads
    events = proposal.events
    transaction = TransactionRange(event.transaction_id,
        min(item.event_id for item in events), max(item.event_id for item in events))
    reads = None if scope == 'missing' else SqlEventReads(owned.connection,
        PlanReadScope(2 if scope == 'wrong_plan' else 1, 128))
    context = EventContext(transaction, proposal.owners, proposal.state_rows,
        proposal.transaction_rows, proposal.read_coverage, current_reads=reads)
    return validate_event(event, context)


async def diagnostic_tool_result(mode):
    """公开 execute_tool 与 ManagedProcess 合同组合产生错误原形。"""
    from camctl.host_files.media import MediaToolResult
    from camctl.operations.process import LocalExit, ToolSpec, ToolStartError, execute_tool
    if mode == 'stopped_before_start':
        return MediaToolResult(stopped_before_start=True)

    class Stop:
        def __init__(self):
            self.event = asyncio.Event()
        async def requested(self):
            await self.event.wait()
    class Process:
        output = b'PRIVATE-MEDIA-STDOUT'
        stderr = b'RAW-MEDIA-ERROR\xff'
        output_failure = ({'output_failure': 'output failed', 'close_failure': 'pipe close failed',
            'cancelled_with_errors': 'pipe close failed'}.get(mode))
        def __init__(self):
            self.ended = asyncio.Event()
            self.terminate_requests = self.kill_requests = 0
        async def wait(self):
            await self.ended.wait()
            return LocalExit(exit_code=7)
        async def wait_output(self):
            await self.ended.wait()
        async def wait_output_failure(self):
            if self.output_failure is None:
                await asyncio.Future()
        def request_terminate(self):
            self.terminate_requests += 1
            if mode in ('signal_failure', 'cancelled_with_errors'):
                raise PermissionError('signal denied')
            self.ended.set()
        def request_kill(self):
            self.kill_requests += 1
            self.ended.set()
    stop, process = Stop(), Process()
    async def spawn(spec):
        if mode == 'spawn':
            raise OSError(2, 'media tool missing')
        if mode == 'exit':
            asyncio.get_running_loop().call_soon(process.ended.set)
        else:
            stop.event.set()
        return process
    try:
        outcome = await execute_tool(ToolSpec(('media-tool',), None, Decimal('.01')),
            stop=stop, spawner=spawn)
    except ToolStartError as error:
        assert mode == 'spawn'
        assert isinstance(error.__cause__, OSError)
        return MediaToolResult(exception=error)
    assert outcome.exit.exit_code == 7
    if mode in ('signal_failure', 'cancelled_with_errors'):
        assert process.terminate_requests == process.kill_requests == 1
        assert outcome.signal_failures
    return MediaToolResult(outcome=outcome)


def diagnostic_probe(tool):
    from camctl.host_files.media import MediaProbe
    if tool.stopped_before_start:
        message = 'tool_cancelled: 工具尚未启动，停止请求已生效'
    elif tool.exception is not None:
        message = f'tool_unavailable: {tool.exception}'
    else:
        raw = tool.outcome
        message = f'tool_failed: {raw.error + "; " if raw.error else ""}exit={raw.exit.exit_code}'
    return MediaProbe(None, message, tool_diagnostics=tool)


def diagnostic_media_ports(monkeypatch, *, probe=None, artifact=None, successful_probe=None):
    """公开媒体文件端口交付指定原结果，真实文件执行器仍拥有文件与工作线程。"""
    from camctl.bootstrap import capture_assembly
    from camctl.host_files.models import BoundDirectories
    from camctl.host_files.tasks import AsyncFileTask
    counts = {'probe': 0, 'repair': 0}
    async def public_probe(input_file, roots, request, *, executor, task_id, owner):
        def observed():
            assert (Path(input_file.root) / input_file.relative_path).read_bytes() == CONTENT
            counts['probe'] += 1
            return probe if probe is not None else successful_probe
        async def body(control):
            return await asyncio.to_thread(observed)
        return await executor.run_async_file_task(AsyncFileTask(task_id, (input_file.file_id,),
            'media_probe', '原媒体诊断输入', body), owner)
    async def public_repair(input_file, output_file, roots, request, *, executor, task_id, owner):
        def observed():
            assert (Path(input_file.root) / input_file.relative_path).read_bytes() == CONTENT
            counts['repair'] += 1
            return artifact
        async def body(control):
            return await asyncio.to_thread(observed)
        return await executor.run_async_file_task(AsyncFileTask(task_id,
            (input_file.file_id, output_file.file_id), 'media_repair_tool', '原媒体诊断输入', body), owner)
    monkeypatch.setattr(capture_assembly, 'probe_media', public_probe)
    monkeypatch.setattr(capture_assembly, 'repair_media', public_repair)
    return counts


def write_tool_evidence(directory, executor):
    """只归档公开文件任务返回的原工具结果，不以持久化摘要替代实际调用。"""
    from camctl.host_files.media import MediaArtifact, MediaProbe
    records = []
    for stage, result in executor.results:
        value = result.value
        if not isinstance(value, (MediaProbe, MediaArtifact)):
            continue
        tool = value.tool_diagnostics
        outcome = None if tool is None else tool.outcome
        records.append({
            'stage': stage, 'value_type': type(value).__name__, 'ran': result.ran,
            'media_error': value.error if isinstance(value, MediaProbe) else value.tool_error,
            'duration': str(value.duration_s) if isinstance(value, MediaProbe) else None,
            'output_size': value.observation.size_bytes if isinstance(value, MediaArtifact) else None,
            'tool_error': None if outcome is None else outcome.error,
            'tool_exit_code': None if outcome is None or outcome.exit is None else outcome.exit.exit_code,
            'tool_stdout_hex': None if outcome is None or outcome.output is None else outcome.output.hex(),
        })
    (Path(directory) / 'actual-tool-outcomes.json').write_text(json.dumps(records, indent=2))


class MediaSaveGate:
    """原工具结果已经形成，真实 worker 在公开保存请求交付前等待。"""
    def __init__(self, monkeypatch, stage):
        import threading
        self.reached = asyncio.Event()
        self.release = threading.Event()
        self.requests = []
        self.used = False
        loop = asyncio.get_running_loop()
        actual = getattr(CaptureRepository, stage)
        def save(repository, request, key, owned):
            self.requests.append((request, key))
            if not self.used:
                self.used = True
                loop.call_soon_threadsafe(self.reached.set)
                if not self.release.wait(20):
                    raise TimeoutError('原媒体结果保存门闩未释放')
            return actual(repository, request, key, owned)
        monkeypatch.setattr(CaptureRepository, stage, save)
