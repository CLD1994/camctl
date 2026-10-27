"""公共诊断的快照、归属与接纳契约；协作者全部使用内存替身。"""

import asyncio
import base64
from datetime import datetime, timezone
import json
import os
from unittest.mock import patch

import pytest

from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.diagnostics import (
    DiagnosticState, DiagnosticStatus, acknowledge_diagnostic, diagnostic_record_for,
    diagnostic_scope, diagnostic_state_for,
    error_diagnostics_context, flush_diagnostics, record_call_outcome, record_error,
)
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.receipt import LogReceipt
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

    def submit(self, record, *, receipt=None):
        receipt = receipt if receipt is not None else LogReceipt()
        self.records.append(record)
        self.complete(record, receipt)
        return receipt

    def complete(self, record, receipt):
        fallback_written = False
        if self.kind is AdmissionKind.CHANNEL_FAILED and record.fallback is not None:
            fallback_written = record.fallback(record.message) is not False
        result = AdmissionResult(AdmissionDecision(self.kind, record.level), 0,
                                 fallback_written=fallback_written)
        receipt.complete_admission(result)
        receipt.complete_write(AdmissionResult(
            result.decision, result.queue_depth,
            delivered=self.kind is AdmissionKind.ACCEPTED,
            fallback_written=fallback_written,
        ))


class GatedChannel(MemoryChannel):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.released = False
        self.pending = []

    def submit(self, record, *, receipt=None):
        receipt = receipt if receipt is not None else LogReceipt()
        self.records.append(record)
        self.entered.set()
        if self.released:
            self.complete(record, receipt)
        else:
            self.pending.append((record, receipt))
        return receipt

    def release_admission(self):
        self.released = True
        for record, receipt in self.pending:
            self.complete(record, receipt)
        self.pending.clear()


def document(channel, index=0):
    return json.loads(channel.records[index].message)


async def test_record_without_command_target_has_no_async_or_output_side_effect():
    with patch("asyncio.create_task", side_effect=AssertionError("不能新建任务")):
        state = record_error(ValueError("input"), stage="read")
        async with diagnostic_scope():
            await flush_diagnostics()
    assert state.status is DiagnosticStatus.NEW


async def test_record_freezes_common_log_metadata_separately_from_business_time():
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        record_error(ValueError("business"), stage="read", context={"occurred_at": "business-time"})
        await flush_diagnostics()
    payload = document(channel)
    assert payload["level"] == "ERROR"
    assert payload["pid"] == os.getpid()
    timestamp = datetime.fromisoformat(payload["logged_at_utc"])
    assert timestamp.utcoffset() == timezone.utc.utcoffset(timestamp)
    assert payload["context"]["occurred_at"] == "business-time"


async def test_background_context_creates_one_consumer_for_all_records():
    channel = MemoryChannel()
    created = []
    real_create_task = asyncio.create_task

    def create_task(coroutine, **kwargs):
        task = real_create_task(coroutine, **kwargs)
        created.append(task)
        return task

    with patch("asyncio.create_task", side_effect=create_task) as create:
        with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
            for number in range(3):
                async with diagnostic_scope():
                    record_error(ValueError(str(number)), stage="read")
            await flush_diagnostics()
    assert create.call_count == 1
    assert len(channel.records) == 3
    assert created[0].done()


async def test_background_scope_never_blocks_later_stop_or_same_device_work():
    channel = GatedChannel()
    operations = []
    with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        async with diagnostic_scope():
            first = record_error(ValueError("first"), stage="read")
        await channel.entered.wait()
        async with diagnostic_scope():
            second = record_error(ValueError("second"), stage="read")
            operations.append("same-device-next-body")
        operations.append("required-stop")
        assert first.status is second.status is DiagnosticStatus.HELD
        channel.release_admission()
        await flush_diagnostics()
    assert operations == ["same-device-next-body", "required-stop"]
    assert first.status is second.status is DiagnosticStatus.ADMITTED


async def test_background_consumer_can_admit_while_business_body_is_still_running():
    admitted = asyncio.Event()

    class SignallingChannel(MemoryChannel):
        def submit(self, record, *, receipt=None):
            result = super().submit(record, receipt=receipt)
            admitted.set()
            return result

    channel = SignallingChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        state = record_error(ValueError("business"), stage="read")
        await admitted.wait()
        assert state.status is DiagnosticStatus.ADMITTED
        await flush_diagnostics()


async def test_background_flush_protects_existing_task_from_repeated_parent_cancel():
    channel = GatedChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        states = [record_error(ValueError(str(number)), stage="cleanup") for number in range(3)]
        await channel.entered.wait()
        flushing = asyncio.create_task(flush_diagnostics())
        await asyncio.sleep(0)
        flushing.cancel("first parent cancellation")
        await asyncio.sleep(0)
        flushing.cancel("repeat parent cancellation")
        await asyncio.sleep(0)
        assert not flushing.done()
        channel.release_admission()
        with pytest.raises(asyncio.CancelledError) as raised:
            await flushing
        assert raised.value.args == ("first parent cancellation",)
        assert all(state.status is DiagnosticStatus.ADMITTED for state in states)
        await flush_diagnostics()
    assert len(channel.records) == 3


async def test_background_serve_control_error_is_owned_until_flush():
    original = KeyboardInterrupt("serve control")
    fallback = []

    class ControlledChannel(MemoryChannel):
        def submit(self, record, *, receipt=None):
            raise original

    with error_diagnostics_context(ControlledChannel(), fallback.append, background=True):
        state = record_error(ValueError("business"), stage="read")
        with pytest.raises(KeyboardInterrupt) as raised:
            await flush_diagnostics()
        assert raised.value is original
        await flush_diagnostics()
    assert state.status is DiagnosticStatus.FALLBACK
    assert len(fallback) == 1


async def test_background_serve_admission_cancel_is_reported_after_remaining_records():
    original = asyncio.CancelledError("serve admission cancellation")
    accepted = AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, LogLevel.ERROR), 0)

    class ControlledChannel(MemoryChannel):
        def submit(self, record, *, receipt=None):
            receipt = super().submit(record, receipt=receipt)
            if len(self.records) == 1:
                raise original
            return receipt

    channel = ControlledChannel()
    with patch("camctl.logging_runtime.diagnostics.admission_result_for", return_value=accepted):
        with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
            states = [record_error(ValueError(str(number)), stage="read") for number in range(2)]
            with pytest.raises(asyncio.CancelledError) as raised:
                await flush_diagnostics()
            assert raised.value is original
            assert all(state.status is DiagnosticStatus.ADMITTED for state in states)
            await flush_diagnostics()
    assert len(channel.records) == 2


async def test_background_late_record_after_flush_goes_directly_to_fallback():
    channel = MemoryChannel()
    fallback = []
    with error_diagnostics_context(channel, fallback.append, background=True):
        await flush_diagnostics()
        state = record_error(ValueError("late"), stage="late")
        assert state.status is DiagnosticStatus.FALLBACK
    assert len(fallback) == 1
    assert not channel.records


async def test_background_scope_preserves_copy_acknowledgement_before_ordinary_admission():
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        async with diagnostic_scope():
            state = record_error(ValueError("report-copy"), stage="report")
            original_record = diagnostic_record_for(state)
            await asyncio.sleep(0)
            assert not channel.records
            assert original_record is not None
            acknowledge_diagnostic(state)
        await flush_diagnostics()
    assert state.status is DiagnosticStatus.ADMITTED
    assert not channel.records


async def test_background_child_after_context_exit_uses_original_fallback_target():
    channel = MemoryChannel()
    fallback = []
    release = asyncio.Event()

    async def child():
        await release.wait()
        return record_error(ValueError("late-child"), stage="shared_save")

    with error_diagnostics_context(channel, fallback.append, background=True):
        child_task = asyncio.create_task(child())
        await flush_diagnostics()
    release.set()
    state = await child_task
    assert state.status is DiagnosticStatus.FALLBACK
    assert len(fallback) == 1
    assert not channel.records


async def test_background_context_requires_flush_even_if_consumer_already_failed():
    original = KeyboardInterrupt("serve control")

    class ControlledChannel(MemoryChannel):
        def submit(self, record, *, receipt=None):
            raise original

    with pytest.raises(RuntimeError) as raised:
        with error_diagnostics_context(ControlledChannel(), lambda text: None, background=True):
            record_error(ValueError("business"), stage="read")
            await asyncio.sleep(0)
    assert raised.value.__cause__ is original


async def test_background_late_scope_handoff_uses_fallback_after_owner_has_stopped():
    fallback = []
    held = asyncio.Event()
    release = asyncio.Event()

    async def child():
        async with diagnostic_scope():
            state = record_error(ValueError("held-before-stop"), stage="shared_save")
            held.set()
            await release.wait()
        return state

    with error_diagnostics_context(MemoryChannel(), fallback.append, background=True):
        task = asyncio.create_task(child())
        await held.wait()
        await flush_diagnostics()
    release.set()
    state = await task
    assert state.status is DiagnosticStatus.FALLBACK
    assert len(fallback) == 1


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


async def test_actual_copy_append_acknowledges_original_record_without_resending():
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        async with diagnostic_scope():
            state = record_error(ValueError("report"), stage="report")
            original_record = diagnostic_record_for(state)
            assert original_record is not None
            assert json.loads(original_record.message)["error"]["message"] == "report"
            # 副本调用方已经持有实际追加成功凭据时，才执行此交接。
            acknowledge_diagnostic(state)
        await flush_diagnostics()
    assert state.status is DiagnosticStatus.ADMITTED
    assert not channel.records
    assert diagnostic_record_for(state) is original_record


async def test_unwritten_copy_keeps_original_record_for_ordinary_admission():
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        state = record_error(ValueError("report"), stage="report")
        original_record = diagnostic_record_for(state)
        # 未获得追加凭据或副本被抑制时，不执行 acknowledge。
        await flush_diagnostics()
    assert channel.records == [original_record]
    assert state.status is DiagnosticStatus.ADMITTED


async def test_acknowledge_does_not_create_a_record_for_unprepared_state():
    state = DiagnosticState()
    acknowledge_diagnostic(state)
    assert state.status is DiagnosticStatus.NEW
    assert diagnostic_record_for(state) is None


async def test_run_error_exception_owns_explicit_call_diagnostic_state():
    channel = MemoryChannel()
    error = RuntimeError("run")
    state = DiagnosticState()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        record_call_outcome(CallOutcome(), run_error=error, state=state)
        assert diagnostic_state_for(error) is state
        assert record_error(error, stage="fatal") is state
        await flush_diagnostics()
    assert len(channel.records) == 1


async def test_root_flush_finishes_all_records_before_propagating_first_cancellation():
    first_cancel = asyncio.CancelledError("first")
    repeat_cancel = asyncio.CancelledError("repeat")
    accepted = AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, LogLevel.ERROR), 0)

    class CancellingChannel(MemoryChannel):
        def submit(self, record, *, receipt=None):
            receipt = super().submit(record, receipt=receipt)
            if len(self.records) == 1:
                raise first_cancel
            if len(self.records) == 2:
                raise repeat_cancel
            return receipt

    channel = CancellingChannel()
    with patch("camctl.logging_runtime.diagnostics.admission_result_for", return_value=accepted):
        with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
            states = [record_error(ValueError(str(index)), stage="root") for index in range(3)]
            with pytest.raises(asyncio.CancelledError) as raised:
                await flush_diagnostics()
            assert raised.value is first_cancel
            assert all(state.status is DiagnosticStatus.ADMITTED for state in states)
            await flush_diagnostics()
    assert len(channel.records) == 3


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


@pytest.mark.parametrize("fallback_result", [None, False])
async def test_unavailable_admission_falls_back_once_with_complete_diagnostic(fallback_result):
    channel = MemoryChannel(AdmissionKind.CHANNEL_FAILED)
    fallback = []

    def write_fallback(message):
        fallback.append(message)
        return fallback_result

    with error_diagnostics_context(channel, write_fallback):
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
        def submit(self, record, *, receipt=None):
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
        def submit(self, record, *, receipt=None):
            receipt = super().submit(record, receipt=receipt)
            if len(self.records) == 1:
                raise original
            return receipt

    channel = CancellingChannel()
    with patch("camctl.logging_runtime.diagnostics.admission_result_for", return_value=accepted):
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


async def test_exception_message_formatting_failure_does_not_replace_business_error():
    class UnprintableError(DiagnosticError):
        def __str__(self):
            raise ValueError("exception message formatting failed")

    error = UnprintableError("original failure", diagnostic_details={"stderr_hex": "6b6e6f776eff"})
    channel = MemoryChannel()
    fallback = []
    with error_diagnostics_context(channel, fallback.append):
        state = record_error(error, stage="read")
        await flush_diagnostics()
    assert state.status is DiagnosticStatus.FALLBACK
    assert len(fallback) == 1
    payload = json.loads(fallback[0])
    assert payload["diagnostic"]["error"]["type"].endswith("UnprintableError")
    assert payload["diagnostic"]["error"]["arguments"] == ["original failure"]
    assert payload["diagnostic"]["error"]["diagnostic_details"]["stderr_text"] == "known\\xff"
    assert payload["diagnostic_format_error"]["type"] == "builtins.ValueError"
    assert "exception message formatting failed" in payload["diagnostic_format_error"]["arguments"]
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

        def submit(self, record, *, receipt=None):
            if not self.cancelled:
                self.cancelled = True
                raise original
            return super().submit(record, receipt=receipt)

    channel = CancellingChannel()
    with patch("camctl.logging_runtime.diagnostics.admission_result_for", return_value=None):
        with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
            with pytest.raises(asyncio.CancelledError) as raised:
                async with diagnostic_scope():
                    state = record_error(ValueError("read"), stage="read")
            assert raised.value is original
            assert state.status is DiagnosticStatus.HELD
            await flush_diagnostics()
    assert state.status is DiagnosticStatus.ADMITTED
    assert len(channel.records) == 1
