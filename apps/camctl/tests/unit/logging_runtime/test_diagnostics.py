"""公共诊断的快照、归属与接纳契约；协作者全部使用内存替身。"""

import asyncio
import base64
import json
from unittest.mock import patch

import pytest

from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.diagnostics import (
    DiagnosticState, DiagnosticStatus, diagnostic_scope, diagnostic_state_for,
    error_diagnostics_context, flush_diagnostics, record_call_outcome, record_error,
)
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import AdmissionResult
from camctl.operations.error_diagnostics import DiagnosticError
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallInfo, CallOutcome, EffectState, ErrorValue,
)
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.operations.tool_diagnostics import (
    ToolCallOutput, ToolDiagnostics, ToolStream, ToolStreamState,
)

pytestmark = pytest.mark.asyncio


class MemoryChannel:
    def __init__(self, kind=AdmissionKind.ACCEPTED):
        self.kind = kind
        self.records = []

    async def alog(self, record):
        self.records.append(record)
        return AdmissionResult(AdmissionDecision(self.kind, record.level), 0)


def document(channel, index=0):
    return json.loads(channel.records[index].message)


async def test_record_without_command_target_has_no_async_or_output_side_effect():
    with patch("asyncio.create_task", side_effect=AssertionError("不能新建任务")):
        state = record_error(ValueError("input"), stage="read")
        async with diagnostic_scope():
            await flush_diagnostics()
    assert state.status is DiagnosticStatus.NEW


async def test_scope_prepares_snapshot_and_waits_after_business_body():
    channel = MemoryChannel()
    details = {"nested": ["original"]}
    context = {"run_id": 7}
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        async with diagnostic_scope():
            state = record_error(ErrorValue("E", "read", details), stage="read", context=context)
            assert state.status is DiagnosticStatus.HELD
            assert not channel.records
            details["nested"].append("late")
            context["run_id"] = 9
        assert state.status is DiagnosticStatus.ADMITTED
    assert document(channel)["error"]["details"] == {"nested": ["original"]}
    assert document(channel)["context"] == {"run_id": 7}
    assert channel.records[0].level is LogLevel.ERROR


async def test_original_state_is_reused_while_held_and_after_admission():
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        async with diagnostic_scope():
            state = record_error(ValueError("original"), stage="read")
            assert record_error(ValueError("replacement"), stage="save", state=state) is state
        assert record_error(ValueError("replacement"), stage="save", state=state) is state
        await flush_diagnostics()
    assert len(channel.records) == 1
    assert document(channel)["error"]["message"] == "original"


async def test_original_exception_owns_state_without_an_explicit_result_state():
    channel = MemoryChannel()
    error = ValueError("original")
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        async with diagnostic_scope():
            state = record_error(error, stage="dispatch")
            assert diagnostic_state_for(error) is state
            assert record_error(error, stage="fatal") is state
        assert record_error(error, stage="command") is state
        await flush_diagnostics()
    assert len(channel.records) == 1
    assert document(channel)["stage"] == "dispatch"


async def test_distinct_exception_with_same_message_gets_distinct_diagnostic():
    channel = MemoryChannel()
    first = ValueError("same")
    second = ValueError("same")
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        record_error(first, stage="read")
        record_error(second, stage="read")
        await flush_diagnostics()
    assert len(channel.records) == 2
    assert diagnostic_state_for(first) is not diagnostic_state_for(second)


async def test_no_target_leaves_original_exception_unmodified():
    error = ValueError("business")
    assert diagnostic_state_for(error) is None
    record_error(error, stage="read")
    assert diagnostic_state_for(error) is None


async def test_scope_hands_off_in_record_order_without_write_receipts():
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        async with diagnostic_scope():
            for number in range(3):
                record_error(ValueError(str(number)), stage="read")
    assert [document(channel, i)["error"]["message"] for i in range(3)] == ["0", "1", "2"]


async def test_exception_scope_propagates_original_before_any_admission_then_root_flushes():
    channel = MemoryChannel()
    original = RuntimeError("business")
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        with pytest.raises(RuntimeError) as raised:
            async with diagnostic_scope():
                state = record_error(ValueError("read"), stage="read")
                raise original
        assert raised.value is original
        assert not channel.records
        assert state.status is DiagnosticStatus.HELD
        await flush_diagnostics()
    assert state.status is DiagnosticStatus.ADMITTED


async def test_late_child_record_after_scope_closed_is_owned_by_root():
    channel = MemoryChannel()
    release = asyncio.Event()

    async def child():
        await release.wait()
        return record_error(ValueError("late"), stage="shared_save")

    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        async with diagnostic_scope():
            task = asyncio.create_task(child())
        release.set()
        state = await task
        assert not channel.records
        await flush_diagnostics()
    assert state.status is DiagnosticStatus.ADMITTED


async def test_successful_call_with_run_error_still_records_all_known_facts():
    channel = MemoryChannel()
    outcome = CallOutcome(call_info=CallInfo(local_exit_code=0), effect=EffectState.CONFIRMED)
    ticket = AttemptTicket(2, "READ", "3", "action/4", 5)
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        async with diagnostic_scope():
            state = record_call_outcome(outcome, ticket=ticket, run_error=ErrorValue("E", "save"))
    assert state.status is DiagnosticStatus.ADMITTED
    payload = document(channel)
    assert payload["outcome"]["status"] == "succeeded"
    assert payload["outcome"]["effect"] == "confirmed"
    assert payload["outcome"]["call_info"]["local_exit_code"] == 0
    assert payload["ticket"]["run_id"] == 5
    assert payload["run_error"]["stage"] == "save"


async def test_call_error_and_run_error_are_one_diagnostic_with_both_facts():
    channel = MemoryChannel()
    outcome = CallOutcome(status=AttemptStatus.FAILED, error=ErrorValue("E1", "read"))
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        async with diagnostic_scope():
            record_call_outcome(outcome, run_error=ValueError("save"))
    assert len(channel.records) == 1
    assert document(channel)["outcome"]["error"]["code"] == "E1"
    assert document(channel)["run_error"]["message"] == "save"


async def test_call_without_error_has_no_diagnostic():
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        assert record_call_outcome(CallOutcome()) is None
        await flush_diagnostics()
    assert not channel.records


async def test_complete_exception_graph_and_nested_details_are_frozen():
    channel = MemoryChannel()
    details = {"known": [1]}
    cause = DiagnosticError("cause", diagnostic_details=details)
    leaf = ValueError("leaf")
    leaf.__cause__ = cause
    leaf.add_note("known note")
    group = ExceptionGroup("all", [leaf, cause])
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        state = record_error(group, stage="background")
        details["known"].append(2)
        await flush_diagnostics()
    error = document(channel)["error"]
    assert len(error["exceptions"]) == 2
    assert error["exceptions"][0]["notes"] == ["known note"]
    assert error["exceptions"][1]["diagnostic_details"] == {"known": [1]}
    assert error["exceptions"][0]["cause"] == {"$ref": "#/exceptions/1"}
    assert state.status is DiagnosticStatus.ADMITTED


async def test_tool_stderr_is_complete_and_readable_but_stdout_bytes_are_not_logged():
    channel = MemoryChannel()
    raw = ("中文\x00failure".encode() + b"\xff") * 100
    diagnostics = ToolDiagnostics(1, (ToolCallOutput(1, "read", ToolStream(ToolStreamState.COMPLETE, b"MEDIA-SECRET"), ToolStream(ToolStreamState.COMPLETE, raw)),))
    outcome = CallOutcome(status=AttemptStatus.FAILED, error=ErrorValue("E", "read"), diagnostics=diagnostics)
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        record_call_outcome(outcome)
        await flush_diagnostics()
    streams = document(channel)["outcome"]["diagnostics"]["calls"][0]
    assert streams["stderr"]["hex"] == raw.hex()
    assert streams["stderr"]["text"] == raw.decode("utf-8", "backslashreplace")
    assert streams["stdout"] == {"state": "complete", "byte_count": 12}
    assert "MEDIA-SECRET" not in channel.records[0].message
    assert b"MEDIA-SECRET".hex() not in channel.records[0].message


@pytest.mark.parametrize("encoding", ["hex", "base64"])
async def test_stderr_in_existing_error_details_gets_readable_text(encoding):
    channel = MemoryChannel()
    raw = b"problem\xff"
    encoded = raw.hex() if encoding == "hex" else base64.b64encode(raw).decode()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        record_error(ErrorValue("E", "read", {"nested": {f"stderr_{encoding}": encoded}}), stage="read")
        await flush_diagnostics()
    nested = document(channel)["error"]["details"]["nested"]
    assert nested[f"stderr_{encoding}"] == encoded
    assert nested["stderr_text"] == "problem\\xff"


@pytest.mark.parametrize("kind", [AdmissionKind.CHANNEL_FAILED, AdmissionKind.FILTERED_OUT])
async def test_unavailable_admission_falls_back_once_with_complete_diagnostic(kind):
    channel = MemoryChannel(kind)
    fallback = []
    with error_diagnostics_context(channel, fallback.append):
        state = record_error(ValueError("original"), stage="read")
        await flush_diagnostics()
        record_error(ValueError("replacement"), stage="read", state=state)
        await flush_diagnostics()
    assert state.status is DiagnosticStatus.FALLBACK
    assert len(fallback) == 1
    assert json.loads(fallback[0])["error"]["message"] == "original"


async def test_missing_channel_uses_fallback():
    fallback = []
    with error_diagnostics_context(None, fallback.append):
        record_error(ValueError("early"), stage="configuration")
        await flush_diagnostics()
    assert json.loads(fallback[0])["stage"] == "configuration"


async def test_log_exception_uses_fallback_without_replacing_business_result():
    class FailingChannel(MemoryChannel):
        async def alog(self, record):
            raise OSError("log disabled")

    fallback = []
    with error_diagnostics_context(FailingChannel(), fallback.append):
        async with diagnostic_scope():
            record_error(ValueError("business"), stage="read")
    assert json.loads(fallback[0])["error"]["message"] == "business"


async def test_fallback_io_failure_is_not_retried():
    calls = []

    def fallback(text):
        calls.append(text)
        raise OSError("stderr closed")

    with error_diagnostics_context(None, fallback):
        state = record_error(ValueError("business"), stage="read")
        await flush_diagnostics()
        await flush_diagnostics()
    assert len(calls) == 1
    assert state.status is DiagnosticStatus.FALLBACK


async def test_control_exception_from_fallback_propagates_and_keeps_pending_diagnostic():
    original = KeyboardInterrupt("stop")
    calls = []

    def fallback(text):
        calls.append(text)
        if len(calls) == 1:
            raise original

    with error_diagnostics_context(None, fallback):
        state = record_error(ValueError("business"), stage="read")
        with pytest.raises(KeyboardInterrupt) as raised:
            await flush_diagnostics()
        assert raised.value is original
        assert state.status is DiagnosticStatus.HELD
        await flush_diagnostics()
    assert state.status is DiagnosticStatus.FALLBACK


async def test_cancelled_admission_remembers_actual_acceptance_and_preserves_remaining():
    original = asyncio.CancelledError("cancel")
    accepted = AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, LogLevel.ERROR), 0)

    class CancellingChannel(MemoryChannel):
        async def alog(self, record):
            self.records.append(record)
            if len(self.records) == 1:
                raise original
            return accepted

    channel = CancellingChannel()
    with patch("camctl.logging_runtime.diagnostics._admission_result_for", return_value=accepted):
        with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
            with pytest.raises(asyncio.CancelledError) as raised:
                async with diagnostic_scope():
                    first = record_error(ValueError("first"), stage="read")
                    second = record_error(ValueError("second"), stage="save")
            assert raised.value is original
            assert first.status is DiagnosticStatus.ADMITTED
            assert second.status is DiagnosticStatus.HELD
            await flush_diagnostics()
    assert len(channel.records) == 2
    assert second.status is DiagnosticStatus.ADMITTED


async def test_formatter_failure_uses_fallback_with_untruncated_original_details():
    channel = MemoryChannel()
    fallback = []
    value = "all details " * 1000
    with patch("camctl.logging_runtime.diagnostics.json.dumps", side_effect=ValueError("format")):
        with error_diagnostics_context(channel, fallback.append):
            state = record_error(ValueError("business"), stage="read", details={"value": value})
            await flush_diagnostics()
    assert state.status is DiagnosticStatus.FALLBACK
    assert value in fallback[0]
    assert "business" in fallback[0]
    assert not channel.records


async def test_tool_encoding_failure_falls_back_with_original_bytes_and_no_media_stdout():
    channel = MemoryChannel()
    fallback = []
    diagnostics = ToolDiagnostics(1, (ToolCallOutput(1, "read", ToolStream(ToolStreamState.COMPLETE, b"MEDIA-SECRET"), ToolStream(ToolStreamState.COMPLETE, b"problem\xff")),))
    outcome = CallOutcome(status=AttemptStatus.FAILED, error=ErrorValue("E", "read"), diagnostics=diagnostics)
    with patch("camctl.logging_runtime.diagnostics.encode_diagnostics", side_effect=ValueError("format")):
        with error_diagnostics_context(channel, fallback.append):
            state = record_call_outcome(outcome)
            await flush_diagnostics()
    assert state.status is DiagnosticStatus.FALLBACK
    assert "70726f626c656dff" in fallback[0]
    assert "problem" in fallback[0]
    assert "MEDIA-SECRET" not in fallback[0]
    assert b"MEDIA-SECRET".hex() not in fallback[0]
    assert not channel.records


async def test_raw_tool_stdout_is_not_exposed_by_generic_dataclass_snapshot():
    channel = MemoryChannel()
    raw = RawToolOutcome(LocalExit(exit_code=1), b"MEDIA-SECRET", "output_failed", None, stderr=b"problem\xff")
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        record_error(ValueError("read"), stage="read", details={"tool_outcome": raw})
        await flush_diagnostics()
    snapshot = document(channel)["details"]["tool_outcome"]
    assert snapshot["output"] == {"byte_count": 12}
    assert snapshot["stderr"] == {"hex": "70726f626c656dff", "text": "problem\\xff"}
    assert snapshot["exit"]["exit_code"] == 1
    assert b"MEDIA-SECRET".hex() not in channel.records[0].message


async def test_unadmitted_cancel_keeps_state_for_root_without_replacing_cancellation():
    original = asyncio.CancelledError("cancel")
    accepted = AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, LogLevel.ERROR), 0)

    class CancellingChannel(MemoryChannel):
        cancelled = False

        async def alog(self, record):
            if not self.cancelled:
                self.cancelled = True
                raise original
            self.records.append(record)
            return accepted

    channel = CancellingChannel()
    with patch("camctl.logging_runtime.diagnostics._admission_result_for", return_value=None):
        with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
            with pytest.raises(asyncio.CancelledError) as raised:
                async with diagnostic_scope():
                    state = record_error(ValueError("read"), stage="read")
            assert raised.value is original
            assert state.status is DiagnosticStatus.HELD
            await flush_diagnostics()
    assert state.status is DiagnosticStatus.ADMITTED
    assert len(channel.records) == 1
