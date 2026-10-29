"""READ 实际结果保留完整异常及停止事实，不使用真实协作者。"""

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture import media_flow
from camctl.capture.input_copy import InputPhase, InputStep
from camctl.devices.read_session import ReadEnd
from camctl.host_files.segments import SegmentFailure, SegmentResult
from camctl.operations.attempts import AttemptConfig, RetryWaitGate
from camctl.operations.models import AttemptTicket
from camctl.operations.error_diagnostics import DiagnosticError
from camctl.outputs import obtain_flow
from camctl.outputs.copy import CopySegmentError
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import OwnedConnection


@pytest.mark.asyncio
@pytest.mark.parametrize('consumer', ['internal', 'device', 'local'])
@pytest.mark.parametrize('initial_kind', [DbOutcomeKind.COMPLETED, DbOutcomeKind.UNKNOWN])
@pytest.mark.parametrize('error_kind', ['plain', 'structured', 'segment', 'camera'])
async def test_read_result_consumer_keeps_exception_and_read_end_on_original_request(
        monkeypatch, consumer, initial_kind, error_kind):
    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    operations = create_autospec(OperationRepository, instance=True, spec_set=True)
    owned = create_autospec(OwnedConnection, instance=True, spec_set=True)
    async def write(callback, **_options):
        return callback(owned)
    database.write.side_effect = write
    database.read.return_value = (None, None) if consumer == 'internal' else False
    operations.finish_attempt.return_value = DbOutcome(initial_kind)
    monkeypatch.setattr(media_flow, 'OperationRepository', lambda: operations)
    runtime = SimpleNamespace(database=database, operations=operations,
        pending_read_results={}, pending_read_business={}, pending_read_ends={},
        continuing_read_tickets={}, pending_segment_inspection_results={}, occurred_at=lambda: 123,
        evidence=obtain_flow._LOCAL_READ_EVIDENCE, retry_gate=RetryWaitGate(),
        monotonic_ns=lambda: 456)
    ticket = AttemptTicket(1, 'read', '2', 'read/2', 5)
    cause = OSError(5, '原始读取错误', 'source.bin')
    raw = {'command': ['adb', '-s', 'cam', 'exec-out'], 'stdout_base64': 'AP8=',
           'stderr_base64': '/wAK', 'local_exit_code': 17, 'remote_exit_code': 9}
    error = (DiagnosticError('段传输失败', diagnostic_details=raw) if error_kind == 'structured' else
             RuntimeError('段传输失败'))
    if error_kind == 'segment':
        result = SegmentResult(31, False, SegmentFailure.READ_FAILED, cause, False,
            ReadEnd(True, 31, 'source_error'), OSError(16, '源关闭故障'))
        error = CopySegmentError('segment_transfer_failed', 'read_failed', source_failed=True, result=result)
    elif error_kind == 'camera':
        from camctl.devices.drivers.hasselblad.driver import CameraReadError
        error = CameraReadError('preparation_failed', [{'name': 'download', 'diagnostic': raw}], causes=(cause,))
    error.__cause__ = cause
    error.add_note('原始诊断备注')
    step = InputStep(InputPhase.SEGMENT_FAILED, copy_id=2, error=error,
        source_failed=True, read_end=ReadEnd(True, 31, 'source_error'), stop_requested=True)
    facts = {'id': 2, 'delivery_status': 1, 'source_device_file_id': 7}
    if consumer == 'internal':
        call = media_flow._finish_internal_read_result(runtime, ticket, step, max_attempts=3)
    elif consumer == 'device':
        assembly = SimpleNamespace(evidence=runtime.evidence, max_read_attempts=3)
        call = obtain_flow._finish_read_attempt(runtime, assembly, facts, ticket, step)
    else:
        call = obtain_flow._finish_local_attempt(runtime, facts, ticket, step, AttemptConfig(3))
    if initial_kind is DbOutcomeKind.UNKNOWN:
        from camctl.contracts.values import ConsistencyError
        with pytest.raises(ConsistencyError):
            await call
    else:
        await call
    original, key = operations.finish_attempt.call_args.args[:2]
    details = original.outcome.outcome.error.details
    if error_kind == 'camera':
        assert details['reason'].startswith('相机文件读取失败: preparation_failed; steps=')
    else:
        assert details['reason'] == ('segment_transfer_failed: read_failed' if error_kind == 'segment' else '段传输失败')
    assert details['exception']['type'] == {
        'plain': 'builtins.RuntimeError',
        'structured': 'camctl.operations.error_diagnostics.DiagnosticError',
        'segment': 'camctl.outputs.copy.CopySegmentError',
        'camera': 'camctl.devices.drivers.hasselblad.driver.CameraReadError',
    }[error_kind]
    assert details['exception']['notes'] == ['原始诊断备注']
    assert details['exception']['cause']['errno'] == 5
    assert details['exception']['cause']['filename'] == 'source.bin'
    if error_kind == 'structured':
        assert details['exception']['diagnostic_details'] == raw
    elif error_kind == 'segment':
        segment = details['exception']['diagnostic_details']['segment_result']
        assert segment['processed_end'] == 31 and segment['write_extent_known'] is False
        assert segment['source_close_error']['errno'] == 16
        assert segment['failure']['errno'] == 5
        assert segment['source_end'] == {'bytes_read': 31, 'error': 'source_error', 'stopped': True}
    elif error_kind == 'camera':
        camera = details['exception']['diagnostic_details']
        assert camera['steps'] == [{'name': 'download', 'diagnostic': raw}]
        assert camera['causes'][0]['errno'] == 5
    assert details['read_end'] == {'bytes_read': 31, 'error': 'source_error', 'stopped': True}
    assert original.outcome.outcome.error.code == ('local_read_failed' if consumer == 'local' else 'device_error')
    assert original.retry_wait is True and original.run_finish is None
    if initial_kind is DbOutcomeKind.UNKNOWN:
        operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
        if consumer == 'internal':
            await media_flow.resume_prepared_internal_reads(database,
                pending_read_results=runtime.pending_read_results,
                pending_read_business=runtime.pending_read_business,
                pending_read_ends=runtime.pending_read_ends,
                continuing_read_tickets=runtime.continuing_read_tickets,
                pending_segment_inspection_results=runtime.pending_segment_inspection_results)
        else:
            database.read.return_value = facts
            await obtain_flow._retry_read_results(runtime)
        assert operations.finish_attempt.call_count == 2
        assert all(call.args[:2] == (original, key) for call in operations.finish_attempt.call_args_list)
    assert not runtime.pending_read_results
