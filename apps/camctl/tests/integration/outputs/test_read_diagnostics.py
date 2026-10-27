"""读取错误随原结果持有，日志接纳不进入原业务保存阶段。"""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl.capture import media_flow
from camctl.capture.input_copy import InputPhase, InputStep
from camctl.logging_runtime.diagnostics import (
    diagnostic_scope, error_diagnostics_context, flush_diagnostics,
)
from camctl.operations.attempts import AttemptConfig, RetryWaitGate
from camctl.operations.models import AttemptTicket
from camctl.outputs import obtain_flow
from camctl.outputs.copy import RecopyExhausted
from camctl.outputs.read_attempts import ReadContinuation, ReadDispatchPrevented, prevented_read_result
from camctl.devices.drivers.hasselblad.driver import CameraReadError
from camctl.devices.read_session import ReadEnd
from camctl.outputs.read_attempts import stopped_read_result
from support.logging import recording_log_channel


def runtime():
    return SimpleNamespace(pending_read_results={}, pending_read_ends={},
        pending_read_business={}, continuing_read_tickets={}, occurred_at=lambda: 123,
        max_read_attempts=3, evidence=obtain_flow._LOCAL_READ_EVIDENCE,
        database=SimpleNamespace(read=AsyncMock(return_value=False)), retry_gate=RetryWaitGate())


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["device", "host", "internal"])
@pytest.mark.parametrize("save_failed", [False, True])
async def test_actual_read_error_is_held_before_result_save(monkeypatch, consumer, save_failed):
    flow = runtime()
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    error = OSError(5, "actual source read failed", "/source/original.mov")
    step = InputStep(InputPhase.SEGMENT_FAILED, copy_id=2, error=error, source_failed=True)
    save = AsyncMock(side_effect=RuntimeError("result not saved") if save_failed else None)
    monkeypatch.setattr(obtain_flow, "_save_read_result_and_settle", save)
    monkeypatch.setattr(media_flow, "_save_internal_read_and_settle", save)
    facts = {"id": 2, "output_id": 7, "action_id": 11,
        "source_device_file_id": 13, "source_intermediate_file_id": None}
    records = []
    channel = recording_log_channel(records)
    fallback = []
    with error_diagnostics_context(channel, fallback.append):
        try:
            async with diagnostic_scope():
                if consumer == "device":
                    await obtain_flow._finish_read_attempt_phase(flow,
                        SimpleNamespace(evidence=flow.evidence, max_read_attempts=3), facts, ticket, step)
                elif consumer == "host":
                    await obtain_flow._finish_local_attempt_phase(flow, facts, ticket, step,
                        AttemptConfig(3, Decimal(10), Decimal(3)))
                else:
                    await media_flow._finish_internal_read_result(flow, ticket, step, max_attempts=3)
                assert not records
        except RuntimeError as failure:
            assert save_failed and str(failure) == "result not saved"
        await flush_diagnostics()
    assert len(records) == 1 and not fallback
    recorded = json.loads(records[0].message)
    assert "actual source read failed" in json.dumps(recorded)
    assert "/source/original.mov" in json.dumps(recorded)
    assert recorded["ticket"]["run_id"] == 5
    assert recorded["ticket"]["attempt_id"] == 1
    assert flow.pending_read_results[(5, 1)].diagnostic is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["device", "host", "internal"])
async def test_checksum_exhaustion_records_verification_error(monkeypatch, consumer):
    flow = runtime()
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    step = InputStep(InputPhase.CHECKSUM_EXHAUSTED, copy_id=2, error=RecopyExhausted(1, 1))
    monkeypatch.setattr(obtain_flow, "_save_read_result_and_settle", AsyncMock())
    monkeypatch.setattr(media_flow, "_save_internal_read_and_settle", AsyncMock())
    records = []
    channel = recording_log_channel(records)
    with error_diagnostics_context(channel, lambda _: None):
        async with diagnostic_scope():
            if consumer == "device":
                await obtain_flow._finish_read_attempt_phase(flow,
                    SimpleNamespace(evidence=flow.evidence, max_read_attempts=3),
                    {"id": 2, "output_id": 7}, ticket, step)
            elif consumer == "host":
                await obtain_flow._finish_local_attempt_phase(flow, {"id": 2}, ticket, step,
                    AttemptConfig(3, Decimal(10), Decimal(3)))
            else:
                await media_flow._finish_internal_read_result(flow, ticket, step, max_attempts=3)
    assert len(records) == 1
    recorded = json.loads(records[0].message)
    assert recorded["stage"] == "checksum_exhausted"
    assert "RecopyExhausted" in json.dumps(recorded)


@pytest.mark.asyncio
async def test_normal_cancellation_before_dispatch_has_no_error_record():
    flow = runtime()
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    flow.continuing_read_tickets[2] = ReadContinuation(ticket, True)
    records = []
    channel = recording_log_channel(records)
    with error_diagnostics_context(channel, lambda _: None):
        async with diagnostic_scope():
            assert prevented_read_result(flow, ticket,
                ReadDispatchPrevented("canceled_before_dispatch", canceled=True), flow.evidence)
    assert records == []


@pytest.mark.asyncio
async def test_download_read_error_keeps_complete_readable_and_raw_stderr(monkeypatch):
    flow = runtime()
    raw = "USB传输失败\n".encode() + b"\xff" + b"x" * 2048
    error = CameraReadError("download failed", [{"name": "download", "local_exit_code": 17,
                                                "stderr_hex": raw.hex()}])
    monkeypatch.setattr(obtain_flow, "_save_read_result_and_settle", AsyncMock())
    records = []
    channel = recording_log_channel(records)
    with error_diagnostics_context(channel, lambda _: None):
        async with diagnostic_scope():
            await obtain_flow._finish_read_attempt_phase(flow,
                SimpleNamespace(evidence=flow.evidence, max_read_attempts=3), {"id": 2, "output_id": 7},
                AttemptTicket(1, "read", "2", "read/2", 5),
                InputStep(InputPhase.SEGMENT_FAILED, copy_id=2, error=error, source_failed=True))
    assert len(records) == 1
    details = json.loads(records[0].message)["error"]["details"]["exception"]["diagnostic_details"]["steps"][0]
    assert details["stderr_hex"] == raw.hex()
    assert details["stderr_text"] == raw.decode("utf-8", "backslashreplace")


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_write_error", [False, True])
async def test_stopped_source_preserves_real_write_error_without_logging_control_stop(actual_write_error):
    flow = runtime()
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    step = InputStep(InputPhase.SEGMENT_FAILED, copy_id=2, stop_requested=True,
        read_end=ReadEnd(True, 10, "stopped"),
        error=OSError(28, "disk full") if actual_write_error else RuntimeError("stopped"))
    records = []
    channel = recording_log_channel(records)
    with error_diagnostics_context(channel, lambda _: None):
        async with diagnostic_scope():
            original = stopped_read_result(flow, ticket, step, flow.evidence, 123)
            assert original is not None
    assert len(records) == int(actual_write_error)
    if actual_write_error:
        assert json.loads(records[0].message)["error"]["errno"] == 28


@pytest.mark.asyncio
async def test_local_read_normal_stopped_result_is_not_error(monkeypatch):
    flow = runtime()
    monkeypatch.setattr(obtain_flow, "_save_read_result_and_settle", AsyncMock())
    records = []
    channel = recording_log_channel(records)
    with error_diagnostics_context(channel, lambda _: None):
        async with diagnostic_scope():
            await obtain_flow._finish_local_attempt_phase(flow, {"id": 2},
                AttemptTicket(1, "read", "2", "read/2", 5),
                InputStep(InputPhase.SEGMENT_FAILED, copy_id=2, stop_requested=True,
                    read_end=ReadEnd(True, 10, "stopped"), error=RuntimeError("stopped")),
                AttemptConfig(3, Decimal(10), Decimal(3)))
    assert records == []
