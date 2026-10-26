"""清理原 completion 与监督过程的真实异常图共同交付。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest

from camctl.bootstrap.cleanup_assembly import HostArtifacts
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.host_files.models import FilePurpose, FileRef
from camctl.host_files.tasks import FileTaskExecutor, FileTaskId, FileTaskResult
from camctl.operations.error_diagnostics import exception_diagnostic
from camctl.operations.models import AttemptStatus, AttemptTicket
from camctl.outputs.cleanup_flow import LocalArtifactRequest, LocalArtifactTaskFailure
from camctl.session.supervision import OwnedTask, Supervisor, SupervisionResult

pytestmark = pytest.mark.asyncio


def _failure(name):
    try:
        try:
            raise OSError(13, name + ' original OS failure', '/staging/source')
        except OSError as cause:
            raise ValueError(name + ' supervisory failure') from cause
    except ValueError as error:
        return error


def _port(*, interruption=None, no_completion=False, drain_error=None):
    request = LocalArtifactRequest('owned-db', 'delete', 91,
        AttemptTicket(1, 'delete', '91', 'delete/91', 7),
        FileRef(28, FilePurpose.REPAIR_OUTPUT, 'derived/28.bin', Path('/staging')))
    actual = DeviceCallResult((DeviceObservation('file_absent', 1, {'cleanup_item_id': '91'}),), None)
    completion = FileTaskResult(FileTaskId('cleanup:owned-db:delete:7:1'), True, actual)
    handle = SimpleNamespace(wait=AsyncMock(return_value=completion))
    if no_completion:
        handle.wait.side_effect = _failure('fallback')
    executor = create_autospec(FileTaskExecutor, instance=True)
    runner_error = _failure('runner')
    async def run(task, owner, **kwargs):
        owner.retain_handle(handle)
        raise interruption if interruption is not None else runner_error
    executor.run_file_task.side_effect = run
    executor.drain_required.return_value = SupervisionResult((), ())
    if drain_error is not None:
        executor.drain_required.side_effect = drain_error
    port = HostArtifacts(Path('/staging'), executor)
    port._validate_request = Mock()
    return port, request, actual, runner_error, handle


def _nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nodes(item)


def _assert_os_graph(error, *names):
    document = exception_diagnostic(error)
    encoded = json.dumps(document, ensure_ascii=False)
    for name in names:
        assert name + ' supervisory failure' in encoded
    assert any(node.get('errno') == 13 and node.get('traceback') for node in _nodes(document))


async def test_successful_completion_keeps_original_runner_exception_graph():
    port, request, actual, runner, _ = _port()
    with pytest.raises(LocalArtifactTaskFailure) as caught:
        await port.delete(request)
    assert caught.value.actual is actual
    assert isinstance(caught.value, RuntimeError)
    _assert_os_graph(caught.value, 'runner')


async def test_missing_completion_keeps_runner_drain_and_fallback_graphs():
    port, request, _, _, _ = _port(no_completion=True, drain_error=_failure('drain'))
    with pytest.raises(ConsistencyError) as caught:
        await port.delete(request)
    _assert_os_graph(caught.value, 'runner', 'drain', 'fallback')


async def test_cancellation_with_failure_keeps_control_and_original_graph():
    interruption = asyncio.CancelledError('original cancellation')
    interruption.__cause__ = _failure('cancel-cause')
    port, request, actual, _, _ = _port(interruption=interruption)
    with pytest.raises(LocalArtifactTaskFailure) as caught:
        await port.delete(request)
    assert caught.value.actual is actual
    assert caught.value.__cause__ is interruption
    _assert_os_graph(caught.value, 'cancel-cause')


async def test_cancellation_notes_alone_do_not_become_supervision_error():
    interruption = asyncio.CancelledError('original cancellation')
    interruption.add_note('ordinary cancellation note')
    port, request, actual, _, _ = _port(interruption=interruption)
    with pytest.raises(LocalArtifactTaskFailure) as caught:
        await port.delete(request)
    assert caught.value.actual is actual
    assert caught.value.__cause__ is interruption
    assert caught.value.supervision_diagnostic is None


async def test_shared_owner_result_keeps_original_exception_object():
    failure = _failure('shared-owner')
    supervisor = Supervisor()
    owner = SimpleNamespace(take_over=AsyncMock(side_effect=failure))
    token = supervisor.register(owner)
    supervisor.handoff(token, OwnedTask('shared-file', 'result-save', 'cleanup', ('file:28',), None))
    result = await supervisor.drain_required()
    assert result.failed[0].exception is failure
    _assert_os_graph(result.failed[0].exception, 'shared-owner')


async def test_shared_owner_exception_graph_survives_cleanup_collection():
    from camctl.session.supervision import TaskFailure, TaskRecord
    failure = _failure('shared-owner')
    port, request, _, _, _ = _port()
    port._executor.drain_required.return_value = SupervisionResult((), (
        TaskFailure(TaskRecord('other-file', 'save', 'cleanup', ('file:29',)), str(failure), failure),))
    with pytest.raises(LocalArtifactTaskFailure) as caught:
        await port.delete(request)
    _assert_os_graph(caught.value, 'runner', 'shared-owner')
