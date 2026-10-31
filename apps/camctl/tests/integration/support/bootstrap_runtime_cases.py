"""运行配置与资源拥有者用例的正式默认会话装配。"""

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime
from camctl.bootstrap.lifecycle import SystemClock
from camctl.contracts.values import ConsistencyError, to_utc_micros
from camctl.devices.evidence import EvidenceContract
from camctl.operations.recovery import RecoveryBoundary
from camctl.outputs.cleanup_flow import advance_cleanup
from camctl.session.clock import ClockCheckInput

from .session import capture_entry, ResultDriver, run_default_session, session_context


def configured_session(config, catalog, driver, results, evidence, *,
                       photo_check=None, extra_ms=0, stop_config=None, stopper=None):
    """本次配置先成为正式输入，默认生命周期再创建全部流程与线程。"""
    devices = {name: dict(value) for name, value in config.devices.items()}
    if photo_check is not None:
        devices['cam-1']['result_check'] = {
            'max_attempts': photo_check.max_attempts,
            'call_timeout_s': photo_check.timeout_s,
            'retry_interval_s': photo_check.retry_interval_s,
        }
    if 'cam-2' in devices:
        devices['cam-2']['capture'] = {'extra_wait_ms': extra_ms}
    if stop_config is not None:
        devices['cam-1']['recording'] = {
            'max_stop_attempts': stop_config.max_attempts,
            'stop_timeout_s': stop_config.timeout_s,
            'stop_retry_interval_s': stop_config.retry_interval_s,
        }
    entry = capture_entry(driver, evidence.with_contract(EvidenceContract(
        'adb_foreground_recovery', 1, 'control', frozenset())),
        stopper=stopper, results_driver=ResultDriver(results))
    entry = replace(entry, declaration=replace(entry.declaration,
        adb_foreground_recovery_operations=frozenset({'control', 'stop', 'result'})))
    current = replace(config, devices=devices)
    deps = build_runtime(CommandMode.RUN, current, catalog=catalog,
                         recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
    task = asyncio.create_task(run_default_session(deps, None, entry, poll_interval_s=0.1))
    return deps, task


async def advance_cleanup_once(factory, context):
    """窄协作调用公开清理推进，工厂和业务数据库仍为真实实现。"""
    runtime = await factory(context.database)
    assert runtime is not None
    return await advance_cleanup(runtime)


def direct_session_context(config, catalog, **options):
    """正式 SessionContext 专项显式提供已保存下界与部署时钟策略。"""
    def clock_policy(connection):
        lower, = connection.execute(
            'SELECT trusted_time_lower_bound FROM runtime_state WHERE id=1').fetchone()
        return ClockCheckInput(lower, to_utc_micros(f'{config.clock.min_plausible_date} 00:00:00'),
            float(config.clock.recheck_delay_s), config.clock.recheck_count)

    return session_context(config.paths.state_db, catalog=catalog, clock=SystemClock(),
                           clock_policy=clock_policy, **options)


async def wait_session_event(event, task, *, timeout_s=5):
    """保持原成功预算，实际会话先结束时立即交付其完整结果。"""
    if task is None:
        return await asyncio.wait_for(event.wait(), timeout_s)
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait((waiter, task), timeout=timeout_s,
                                    return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            return await waiter
        if task in done:
            result = await task
            raise AssertionError(f'目标事件尚未发生，会话已经结束: {result}')
        raise TimeoutError('目标事件未在原成功预算内发生')
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def apply_host_cleanup_cancel(config, owned, context):
    """线程池被真实占满时，已解析请求仍可经独立数据库线程受理和取消。"""
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import AcceptanceContext, accept_input
    from camctl.bootstrap.flows import cancel_flow
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from ..bootstrap.test_output_binding_changes import Catalog

    target, = owned.connection.execute('SELECT id FROM actions WHERE type=5').fetchone()
    accepted = await accept_input(ParsedInput('cancel-file-task.json', {
        'request_id': '3', 'created_at': '2026-01-15 08:00:00', 'name': '双设备',
        'actions': [{'name': '取消清理', 'type': 'cancel_task',
            'scheduled_at': '2026-01-15 09:00:00',
            'params': {'target': {'action_instance_id': str(target)}}}]}),
        AcceptanceContext(CommandMode.RUN, Catalog(), AcceptanceRepository(), clock=context.clock),
        new_operation_key(), context.database)
    assert accepted.plan_id is not None, accepted
    await cancel_flow(staging=Path(config.paths.staging), ready=Path(config.paths.ready),
        processing=Path(config.paths.processing))(context)


def original_host_calls():
    """保存公开 DELETE/FILE_QUERY 端口，再在调用方观察原请求与结果。"""
    from camctl.bootstrap.cleanup_assembly import HostArtifacts
    delete, query = HostArtifacts.delete, HostArtifacts.query_file

    async def call(port, request):
        return await (delete if request.operation == 'delete' else query)(port, request)

    return call


def observe_host_calls(monkeypatch, callback):
    from camctl.bootstrap.cleanup_assembly import HostArtifacts
    monkeypatch.setattr(HostArtifacts, 'delete', callback)
    monkeypatch.setattr(HostArtifacts, 'query_file', callback)


class ThreadRunnerInput:
    """FileTaskExecutor 的正式线程执行输入，正常实现仍用默认线程池。"""
    def __init__(self):
        self.call = asyncio.to_thread

    async def __call__(self, body):
        return await self.call(body)


@dataclass
class HostFileWorld:
    config: object
    owned: object
    context: object
    driver: object
    executor: object
    supervisor: object
    thread_runner: ThreadRunnerInput
    diagnostics: list = field(default_factory=list)
    port_failures: list = field(default_factory=list)

    def __iter__(self):
        yield from (self.config, self.owned, self.context, self.driver, self.executor, self.supervisor)


def observe_host_failures(monkeypatch, world):
    from camctl.outputs.cleanup_flow import LocalArtifactTaskFailure
    original = original_host_calls()

    async def call(port, request):
        try:
            return await original(port, request)
        except LocalArtifactTaskFailure as failure:
            world.port_failures.append((request, failure))
            raise

    observe_host_calls(monkeypatch, call)


class OwnerHandoffProbe:
    """在 Supervisor 的公开拥有者协议处观察 handle 或提供接手故障。"""
    def __init__(self, monkeypatch, supervisor):
        self.callback = None
        self.handles = []
        register, handoff = supervisor.register, supervisor.handoff
        probe = self

        def registered(owner):
            class Owner:
                async def take_over(self, task):
                    if probe.callback is not None:
                        return await probe.callback(owner, task)
                    return await owner.take_over(task)

            return register(Owner())

        def handed_off(token, task):
            self.handles.append(task.pending)
            return handoff(token, task)

        monkeypatch.setattr(supervisor, 'register', registered)
        monkeypatch.setattr(supervisor, 'handoff', handed_off)


async def check_unknown_cleanup_supervision(world, monkeypatch, operation, factory):
    """原完成结果、未知提交与监督错误经公开清理入口分别交付。"""
    import json
    import sqlite3
    from camctl.bootstrap.cleanup_assembly import cleanup_flow
    from camctl.host_files.tasks import FileTaskId
    from camctl.logging_runtime.diagnostics import flush_diagnostics
    from camctl.operations.error_diagnostics import exception_diagnostic
    from camctl.outputs.cleanup_results import saved_cleanup_delete, saved_cleanup_query
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.persistence.repositories.outputs import OutputsRepository
    from ..bootstrap.cleanup_helpers import cleanup_failure, finish_cleanup_round

    cfg, owned, context, driver, executor, supervisor = world
    target = Path(cfg.paths.staging) / 'derived/900.jpg'
    original_runner = world.thread_runner.call
    submitted, finishes = [], []
    original_run = executor.run_file_task
    repository = OutputsRepository if operation == 'delete' else OperationRepository
    method = 'finish_cleanup_attempt' if operation == 'delete' else 'finish_attempt'
    original_finish = getattr(repository, method)
    diagnosis = f'PROBE {operation.upper()} runner diagnosis survives UNKNOWN'
    unknown_error = f'PROBE {operation.upper()} commit result is unknown'

    async def fail_after_completion(body):
        await original_runner(body)
        raise RuntimeError(diagnosis)

    async def track(task, owner, **options):
        if task.business == 'host_artifact_cleanup':
            submitted.append(task)
        previous = world.thread_runner.call
        if task.stage == f'cleanup_{operation}':
            world.thread_runner.call = fail_after_completion
        try:
            return await original_run(task, owner, **options)
        finally:
            world.thread_runner.call = previous

    def save(current, finish, key, observed):
        receipt = original_finish(current, finish, key, observed)
        if finish.ticket.operation == operation:
            finishes.append((finish, key, receipt))
            if len(finishes) == 1:
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
                return DbOutcome(DbOutcomeKind.UNKNOWN, error=sqlite3.OperationalError(unknown_error))
        return receipt

    monkeypatch.setattr(repository, method, save)
    monkeypatch.setattr(executor, 'run_file_task', track)
    if operation == 'query':
        unlink = Path.unlink
        def keep_present(path, *args, **kwargs):
            if path == target:
                raise PermissionError('PROBE DELETE leaves the artifact for QUERY')
            return unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'unlink', keep_present)
    owner = cleanup_flow(factory)
    with cleanup_failure(ConsistencyError) as first:
        await finish_cleanup_round(owner, context)
    first_graph = json.dumps(exception_diagnostic(first.value), ensure_ascii=False)
    assert unknown_error in first_graph and diagnosis in first_graph
    original, key, receipt = finishes[0]
    ticket, actual, when = original.ticket, original.outcome.outcome, original.occurred_at
    item_id = int(ticket.target_id)
    reader = saved_cleanup_delete if operation == 'delete' else saved_cleanup_query
    saved = reader(owned.connection, item_id)
    assert saved is not None and saved.ticket == ticket and saved.key == key
    assert saved.actual.observations == actual.observations
    assert actual.observations[0].type == ('file_absent' if operation == 'delete' else 'file_presence')
    if operation == 'query':
        assert actual.observations[0].data == {'cleanup_item_id': str(item_id), 'present': True}
    before = owned.connection.execute('SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?',
        (ticket.run_id, ticket.attempt_id)).fetchone()
    assert target.exists() is (operation == 'query')

    with cleanup_failure(ConsistencyError) as recovered:
        await finish_cleanup_round(owner, context)
    assert diagnosis in json.dumps(exception_diagnostic(recovered.value), ensure_ascii=False)
    assert len(finishes) == 2
    assert all(request is original and current_key == key
               and current_receipt.kind is DbOutcomeKind.COMPLETED
               for request, current_key, current_receipt in finishes)
    assert finishes[1][0].outcome.outcome is actual
    assert finishes[1][0].occurred_at == when
    assert owned.connection.execute('SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?',
        (ticket.run_id, ticket.attempt_id)).fetchone() == before
    await flush_diagnostics()
    records = [json.loads(line) for line in world.diagnostics]
    errors = [record for record in records if record.get('stage') == operation
              and diagnosis in json.dumps(record, ensure_ascii=False)]
    assert len(errors) == 1 and errors[0]['level'] == 'ERROR'
    assert errors[0]['context'] == {'database_id': owned.metadata.instance_id,
                                  'cleanup_item_id': item_id}
    assert diagnosis in json.dumps(errors[0], ensure_ascii=False)

    await finish_cleanup_round(owner, context)
    await flush_diagnostics()
    assert len([json.loads(line) for line in world.diagnostics
        if json.loads(line).get('stage') == operation and diagnosis in line]) == 1
    identity = FileTaskId(f'cleanup:{owned.metadata.instance_id}:{operation}:{ticket.run_id}:{ticket.attempt_id}')
    assert [task.task_id for task in submitted].count(identity) == 1
    assert owner.required_settlements() == 0
    assert executor.unfinished_files() == ()
    assert executor.lease_of(identity) is None
    assert target.exists() is (operation == 'query')


async def check_host_port_control(world, monkeypatch, factory, *, scope_inside=False,
                                  cancel_waiter=True, runner_failure=False,
                                  drain_failure=False, unavailable_actual=False,
                                  cancel_label='ORIGINAL_SCOPE_OUTSIDE_CANCEL',
                                  repeat_label='REPEATED_SCOPE_OUTSIDE_CANCEL',
                                  runner_message='PROBE scope-outside runner fault'):
    """正式主机文件端口经真实线程返回，控制与监督故障沿公开错误图交付。"""
    import threading
    from camctl.host_files.models import FilePurpose, FileRef
    from camctl.host_files.tasks import FileTaskHandle
    from camctl.operations.models import AttemptTicket
    from camctl.operations.owned_calls import current_call_scope, owned_tool_call
    from camctl.outputs.cleanup_flow import LocalArtifactRequest, LocalArtifactTaskFailure

    cfg, owned, context, driver, executor, supervisor = world
    staging = Path(cfg.paths.staging)
    target = staging / 'derived/900.jpg'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'host-control-original-bytes')
    ticket = AttemptTicket(1, 'delete', '1', 'delete/1', 1)
    request = LocalArtifactRequest(owned.metadata.instance_id, 'delete', 1, ticket,
        FileRef(900, FilePurpose.REPAIR_OUTPUT, 'derived/900.jpg', staging))
    port = (await factory(context.database)).local_files
    assert port is not None and current_call_scope() is None
    loop = asyncio.get_running_loop()
    body_started, collecting, cancel_seen, repeated_seen, draining = (asyncio.Event() for _ in range(5))
    release_body, release_drain = threading.Event(), asyncio.Event()
    submitted, actuals, workers, cancellations, handles, handle_errors = [], [], [], [], [], []
    collector = None
    captured = {}
    runner_error = RuntimeError(runner_message)
    drain_error = RuntimeError('PROBE shared drain failed after cancellation')
    cancel_cause = RuntimeError('PROBE original cancellation cause')
    original_run = executor.run_file_task
    original_drain, original_wait, original_shield = executor.drain_required, FileTaskHandle.wait, asyncio.shield

    def remember(error):
        if any(error is item for item in cancellations):
            return
        cancellations.append(error)
        if len(cancellations) == 1:
            if runner_failure or drain_failure or unavailable_actual:
                error.add_note('PROBE original cancellation note')
                error.__cause__ = cancel_cause
                error.__suppress_context__ = True
            captured['interruption'] = error
            cancel_seen.set()
        else:
            repeated_seen.set()

    async def run(task, owner, **options):
        nonlocal collector
        collector = asyncio.current_task()
        submitted.append(task)
        original_body = task.body
        def body(stop):
            actual = original_body(stop)
            actuals.append(actual)
            loop.call_soon_threadsafe(body_started.set)
            release_body.wait()
            return actual
        try:
            return await original_run(replace(task, body=body), owner, **options)
        except asyncio.CancelledError as error:
            remember(error)
            collecting.set()
            raise
        except RuntimeError:
            collecting.set()
            raise

    async def fail_runner(body):
        worker = asyncio.create_task(asyncio.to_thread(body))
        workers.append(worker)
        await body_started.wait()
        raise runner_error

    async def drain():
        draining.set()
        if drain_failure or unavailable_actual:
            await release_drain.wait()
        if drain_failure:
            raise drain_error
        return await original_drain()

    async def wait(handle):
        if unavailable_actual and handle.task_id.value == identity:
            handles.append(handle)
            error = RuntimeError('PROBE original handle cannot deliver result')
            handle_errors.append(error)
            raise error
        return await original_wait(handle)

    def shield(awaitable):
        actual = original_shield(awaitable)
        if asyncio.current_task() is not collector or not collecting.is_set():
            return actual
        async def observe():
            try:
                return await actual
            except asyncio.CancelledError as error:
                remember(error)
                raise
        return observe()

    identity = f'cleanup:{request.database_id}:delete:{ticket.run_id}:{ticket.attempt_id}'
    monkeypatch.setattr(executor, 'run_file_task', run)
    monkeypatch.setattr(executor, 'drain_required', drain)
    monkeypatch.setattr(FileTaskHandle, 'wait', wait)
    monkeypatch.setattr(asyncio, 'shield', shield)
    if runner_failure:
        monkeypatch.setattr(world.thread_runner, 'call', fail_runner)
    scopes = []
    @owned_tool_call
    async def owned_call():
        scopes.append(current_call_scope())
        return await port.delete(request)
    call = asyncio.create_task(owned_call() if scope_inside else port.delete(request))
    outcome = None
    try:
        await asyncio.wait_for(body_started.wait(), 5)
        assert executor.unfinished_files() == (900,)
        assert not target.exists()
        if runner_failure:
            await asyncio.wait_for(collecting.wait(), 5)
        if cancel_waiter:
            call.cancel(cancel_label)
            await asyncio.wait_for(cancel_seen.wait(), 5)
            if drain_failure or unavailable_actual:
                await asyncio.wait_for(draining.wait(), 5)
            call.cancel(repeat_label)
            if not scope_inside:
                await asyncio.wait_for(repeated_seen.wait(), 5)
            else:
                await asyncio.sleep(0)
        release_drain.set()
        await asyncio.sleep(0)
        release_body.set()
        try:
            outcome = await call
        except BaseException as error:
            outcome = error
    finally:
        release_body.set()
        release_drain.set()
        if not call.done():
            await asyncio.gather(call, return_exceptions=True)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if handles:
            await original_wait(handles[0])
        await original_drain()

    def graph(error):
        seen, pending, result = set(), [error], []
        while pending:
            current = pending.pop()
            if not isinstance(current, BaseException) or id(current) in seen:
                continue
            seen.add(id(current)); result.append(current)
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions)
            pending.extend((current.__cause__, current.__context__))
        return result

    assert len(submitted) == len(actuals) == 1
    assert submitted[0].task_id.value == identity and submitted[0].file_id == 900
    assert request.ticket is ticket
    assert not target.exists()
    if unavailable_actual:
        assert isinstance(outcome, ConsistencyError)
        assert getattr(outcome, 'actual', None) is None
        assert isinstance(outcome.__cause__, BaseExceptionGroup)
        failures = outcome.__cause__.exceptions
        assert len(failures) == 2 and failures[0] is captured['interruption']
        assert str(captured['interruption']) == cancel_label
        assert isinstance(failures[1], BaseExceptionGroup)
        errors = graph(outcome)
        assert any(error is runner_error for error in errors)
        assert handle_errors and all(any(error is item for error in errors) for item in handle_errors)
    elif not cancel_waiter and not runner_failure and not drain_failure:
        assert outcome is actuals[0]
    else:
        errors = graph(outcome)
        carriers = [error for error in errors if isinstance(error, LocalArtifactTaskFailure)]
        if runner_failure or drain_failure or not scope_inside:
            carrier, = carriers
            assert carrier.actual is actuals[0]
            diagnostic = carrier.supervision_diagnostic
            assert (diagnostic is not None) is (runner_failure or drain_failure)
            if diagnostic is not None:
                errors.extend(graph(diagnostic.exception))
            if runner_failure:
                assert any(error is runner_error for error in errors)
                assert any(fact.source == 'runner' and fact.error_message == str(runner_error)
                           for fact in diagnostic.facts)
            if drain_failure:
                assert any(error is drain_error for error in errors)
                assert any(fact.source == 'shared_drain' and fact.error_message == str(drain_error)
                           for fact in diagnostic.facts)
        if cancel_waiter:
            interruption = captured['interruption']
            if not scope_inside:
                assert str(interruption) == cancel_label
            if runner_failure or drain_failure:
                assert interruption.__cause__ is cancel_cause
                assert 'PROBE original cancellation note' in interruption.__notes__
            assert cancellations[0] is interruption
            if scope_inside:
                assert isinstance(outcome, asyncio.CancelledError)
                assert str(outcome) == cancel_label
                assert scopes[0].interrupted is interruption
            else:
                assert isinstance(outcome, LocalArtifactTaskFailure)
                assert outcome.__cause__ is interruption
                assert len(cancellations) == 2
    assert executor.unfinished_files() == ()
    assert supervisor.outstanding() == ()


def runtime_database_facts(path, stage):
    """小型会话场景的只读原事实，供失败归因及原错误图交接。"""
    import sqlite3
    with sqlite3.connect('file:' + str(Path(path).resolve()) + '?mode=ro', uri=True) as database:
        database.row_factory = sqlite3.Row
        tables = ('actions', 'device_activities', 'cleanup_items', 'operation_runs',
                  'operation_attempts', 'intermediate_files', 'history_events', 'history_transactions')
        return {'stage': stage, 'tables': {table: [dict(row) for row in database.execute(
            f'SELECT * FROM {table} ORDER BY id')] for table in tables}}


def save_runtime_evidence(directory, outcome, snapshots):
    """保存机器结果、原诊断异常及快照；不复制原异常而丢失堆栈。"""
    import json
    from collections.abc import Mapping
    from camctl.logging_runtime.diagnostics import DiagnosticState, diagnostic_record_for
    from camctl.operations.error_diagnostics import exception_diagnostic

    def encode(value, path):
        if isinstance(value, BaseException):
            return encode(exception_diagnostic(value, root_path=path), path)
        if isinstance(value, DiagnosticState):
            record = diagnostic_record_for(value)
            return {"status": value.status.value, "diagnostic_id": value.diagnostic_id,
                    "record": None if record is None else json.loads(record.rendered_message())}
        if isinstance(value, Mapping):
            return {key: encode(item, path + '/' + str(key).replace('~', '~0').replace('/', '~1'))
                    for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [encode(item, path + '/' + str(index)) for index, item in enumerate(value)]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise TypeError(f"不支持的会话证据类型: {type(value).__module__}.{type(value).__qualname__}")

    document = {'outcome': {
        'succeeded': outcome.succeeded, 'needs_run': outcome.needs_run,
        'reason': outcome.reason, 'details': outcome.details,
        '_diagnostic_sources': outcome._diagnostic_sources,
    }, 'snapshots': snapshots}
    Path(directory, 'session-evidence.json').write_text(
        json.dumps(encode(document, '#'), ensure_ascii=False, indent=2), encoding='utf-8')
