"""通用日志通过原诊断状态交接单条记录，不改变其他拥有者。"""

import asyncio
import json
from unittest.mock import patch

import pytest

from camctl.logging_runtime import diagnostics
from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import AdmissionResult, LogRecord
from camctl.operations.process import LocalExit, RawToolOutcome

pytestmark = pytest.mark.asyncio


class MemoryChannel:
    def __init__(self, kind=AdmissionKind.ACCEPTED):
        self.kind = kind
        self.records = []

    async def alog(self, record):
        self.records.append(record)
        return AdmissionResult(AdmissionDecision(self.kind, record.level), 0)


class GatedChannel(MemoryChannel):
    def __init__(self, kind=AdmissionKind.ACCEPTED):
        super().__init__(kind)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def alog(self, record):
        self.entered.set()
        await self.release.wait()
        return await super().alog(record)


def prepared(message="original", *, fallback=None):
    return LogRecord(LogLevel.ERROR, json.dumps({"message": message}),
                     fallback=fallback, structured=True)


async def test_public_snapshot_retains_tool_stderr_without_media_stdout():
    raw = RawToolOutcome(LocalExit(exit_code=1), b"MEDIA-SECRET", None, None, stderr=b"problem\xff")
    snapshot = diagnostics.diagnostic_snapshot({"tool": raw})
    assert snapshot["tool"]["output"] == {"byte_count": 12}
    assert snapshot["tool"]["stderr"] == {"hex": "70726f626c656dff", "text": "problem\\xff"}
    assert "MEDIA-SECRET" not in json.dumps(snapshot)


async def test_prepared_log_has_exception_identity_before_admission_without_new_task():
    channel = MemoryChannel()
    error = ValueError("original")
    record = prepared()
    with patch("asyncio.create_task", side_effect=AssertionError("不能新建逐条任务")):
        state = diagnostics.prepare_log_diagnostic(record, error=error, channel=channel)
        assert diagnostics.diagnostic_state_for(error) is state
        assert state.status is diagnostics.DiagnosticStatus.HELD
        assert not channel.records
        assert await diagnostics.admit_diagnostic(state) is state
    assert state.status is diagnostics.DiagnosticStatus.ADMITTED
    assert [item.message for item in channel.records] == [record.message]


async def test_single_admission_preserves_other_pending_records_and_first_exception_facts():
    channel = MemoryChannel()
    error = ValueError("original")
    with diagnostics.error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        first = diagnostics.record_error(error, stage="first")
        second = diagnostics.prepare_log_diagnostic(prepared("second"))
        original = diagnostics.diagnostic_record_for(first)
        assert diagnostics.prepare_log_diagnostic(
            prepared("replacement"), error=error, state=second,
        ) is first
        await diagnostics.admit_diagnostic(second)
        assert channel.records == [diagnostics.diagnostic_record_for(second)]
        assert first.status is diagnostics.DiagnosticStatus.HELD
        assert diagnostics.diagnostic_record_for(first) is original
        await diagnostics.admit_diagnostic(first)
        assert diagnostics.record_error(error, stage="outer") is first
        await diagnostics.admit_diagnostic(first)
        await diagnostics.flush_diagnostics()
    assert channel.records == [diagnostics.diagnostic_record_for(second), original]


async def test_same_text_without_exception_prepares_independent_states():
    channel = MemoryChannel()
    states = [diagnostics.prepare_log_diagnostic(prepared("same"), channel=channel)
              for _ in range(2)]
    for state in states:
        await diagnostics.admit_diagnostic(state)
    assert states[0] is not states[1]
    assert len(channel.records) == 2


async def test_current_command_target_precedes_explicit_channel_and_fallback():
    command_fallback = []
    unused_fallback = []
    unused_channel = MemoryChannel()
    with diagnostics.error_diagnostics_context(None, command_fallback.append):
        state = diagnostics.prepare_log_diagnostic(
            prepared(fallback=unused_fallback.append), channel=unused_channel,
            fallback=unused_fallback.append,
        )
        await diagnostics.admit_diagnostic(state)
        await diagnostics.flush_diagnostics()
    assert len(command_fallback) == 1
    assert not unused_channel.records
    assert not unused_fallback
    assert state.status is diagnostics.DiagnosticStatus.FALLBACK


@pytest.mark.parametrize("explicit", [False, True])
async def test_standalone_log_preserves_selected_fallback_once(explicit):
    record_fallback = []
    explicit_fallback = []
    state = diagnostics.prepare_log_diagnostic(
        prepared(fallback=record_fallback.append),
        fallback=explicit_fallback.append if explicit else None,
    )
    await diagnostics.admit_diagnostic(state)
    await diagnostics.admit_diagnostic(state)
    assert len(explicit_fallback if explicit else record_fallback) == 1
    assert not (record_fallback if explicit else explicit_fallback)
    assert state.status is diagnostics.DiagnosticStatus.FALLBACK


async def test_standalone_missing_channel_and_fallback_uses_stderr(capsys):
    state = diagnostics.prepare_log_diagnostic(prepared("stderr-fact"))
    await diagnostics.admit_diagnostic(state)
    assert json.loads(capsys.readouterr().err)["message"] == "stderr-fact"
    assert state.status is diagnostics.DiagnosticStatus.FALLBACK


async def test_existing_background_admission_is_awaited_without_resending():
    channel = GatedChannel()
    with diagnostics.error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        state = diagnostics.record_error(ValueError("original"), stage="first")
        await channel.entered.wait()
        waiter = asyncio.create_task(diagnostics.admit_diagnostic(state))
        await asyncio.sleep(0)
        assert not waiter.done()
        channel.release.set()
        assert await waiter is state
        await diagnostics.flush_diagnostics()
    assert len(channel.records) == 1


async def test_waiting_on_other_admitter_preserves_first_repeated_cancellation():
    channel = GatedChannel()
    state = diagnostics.prepare_log_diagnostic(prepared(), channel=channel)
    owner = asyncio.create_task(diagnostics.admit_diagnostic(state))
    await channel.entered.wait()
    waiter = asyncio.create_task(diagnostics.admit_diagnostic(state))
    await asyncio.sleep(0)
    waiter.cancel("first")
    await asyncio.sleep(0)
    waiter.cancel("repeat")
    await asyncio.sleep(0)
    assert not waiter.done()
    channel.release.set()
    assert await owner is state
    with pytest.raises(asyncio.CancelledError) as raised:
        await waiter
    assert raised.value.args == ("first",)
    assert len(channel.records) == 1


async def test_released_reservation_can_admit_without_waiting_for_global_flush():
    channel = MemoryChannel()
    with diagnostics.error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        state = diagnostics.record_error(ValueError("reserved"), stage="report")
        reservation = diagnostics.reserve_diagnostic(state)
        waiter = asyncio.create_task(diagnostics.admit_diagnostic(state))
        await asyncio.sleep(0)
        assert not waiter.done()
        assert not channel.records
        diagnostics.release_diagnostic(reservation)
        assert await waiter is state
        await diagnostics.flush_diagnostics()
    assert len(channel.records) == 1


@pytest.mark.parametrize("acknowledge", [False, True])
async def test_adopted_copy_keeps_ownership_until_acknowledgement_or_scope_handoff(acknowledge):
    channel = MemoryChannel()
    adopted = asyncio.Event()
    release = asyncio.Event()
    with diagnostics.error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        state = diagnostics.record_error(ValueError("copy"), stage="report")
        reservation = diagnostics.reserve_diagnostic(state)

        async def copy_owner():
            async with diagnostics.diagnostic_scope():
                diagnostics.adopt_diagnostic(reservation)
                adopted.set()
                await release.wait()
                if acknowledge:
                    diagnostics.acknowledge_diagnostic(state)

        copy_task = asyncio.create_task(copy_owner())
        await adopted.wait()
        waiter = asyncio.create_task(diagnostics.admit_diagnostic(state))
        await asyncio.sleep(0)
        assert not waiter.done()
        assert not channel.records
        release.set()
        await copy_task
        assert await waiter is state
        await diagnostics.flush_diagnostics()
    assert len(channel.records) == (0 if acknowledge else 1)


async def test_direct_admission_retries_unconfirmed_cancellation_then_propagates_original():
    original = asyncio.CancelledError("first")

    class CancelledChannel(MemoryChannel):
        cancelled = False

        async def alog(self, record):
            if not self.cancelled:
                self.cancelled = True
                raise original
            return await super().alog(record)

    channel = CancelledChannel()
    state = diagnostics.prepare_log_diagnostic(prepared(), channel=channel)
    with pytest.raises(asyncio.CancelledError) as raised:
        await diagnostics.admit_diagnostic(state)
    assert raised.value is original
    assert state.status is diagnostics.DiagnosticStatus.ADMITTED
    assert len(channel.records) == 1


@pytest.mark.parametrize("kind", [AdmissionKind.CHANNEL_FAILED, AdmissionKind.FILTERED_OUT])
async def test_failed_admission_notifies_other_waiter_and_falls_back_once(kind):
    fallback = []
    channel = GatedChannel(kind)
    state = diagnostics.prepare_log_diagnostic(prepared(), channel=channel, fallback=fallback.append)
    owner = asyncio.create_task(diagnostics.admit_diagnostic(state))
    await channel.entered.wait()
    waiter = asyncio.create_task(diagnostics.admit_diagnostic(state))
    await asyncio.sleep(0)
    assert not waiter.done()
    channel.release.set()
    assert await owner is await waiter is state
    assert state.status is diagnostics.DiagnosticStatus.FALLBACK
    assert len(channel.records) == len(fallback) == 1


async def test_direct_admission_after_command_finished_preserves_late_scope_fallback():
    channel = MemoryChannel()
    fallback = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def late_owner():
        async with diagnostics.diagnostic_scope():
            state = diagnostics.record_error(ValueError("late"), stage="late")
            entered.set()
            await release.wait()
            return await diagnostics.admit_diagnostic(state)

    with diagnostics.error_diagnostics_context(channel, fallback.append, background=True):
        task = asyncio.create_task(late_owner())
        await entered.wait()
        await diagnostics.flush_diagnostics()
    release.set()
    state = await task
    assert state.status is diagnostics.DiagnosticStatus.FALLBACK
    assert len(fallback) == 1
    assert not channel.records


async def test_scope_control_without_admission_receipt_hands_original_to_waiter():
    original = SystemExit("admission control")

    class ControlledChannel(GatedChannel):
        controlled = False

        async def alog(self, record):
            if not self.controlled:
                self.controlled = True
                self.entered.set()
                await self.release.wait()
                raise original
            return await super().alog(record)

    channel = ControlledChannel()
    states = []

    async def original_owner():
        with pytest.raises(SystemExit) as raised:
            async with diagnostics.diagnostic_scope():
                states.append(diagnostics.record_error(ValueError("original"), stage="read"))
        return raised.value

    with diagnostics.error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        owner = asyncio.create_task(original_owner())
        await channel.entered.wait()
        waiter = asyncio.create_task(diagnostics.admit_diagnostic(states[0]))
        await asyncio.sleep(0)
        assert not waiter.done()
        channel.release.set()
        assert await owner is original
        assert await waiter is states[0]
        await diagnostics.flush_diagnostics()
    assert states[0].status is diagnostics.DiagnosticStatus.ADMITTED
    assert len(channel.records) == 1


async def test_late_preparation_binds_exception_before_fallback_control_propagates():
    original = asyncio.CancelledError("stderr control")
    error = ValueError("original")
    calls = []

    def fallback(message):
        calls.append(message)
        raise original

    with diagnostics.error_diagnostics_context(None, fallback, background=True):
        await diagnostics.flush_diagnostics()
        with pytest.raises(asyncio.CancelledError) as raised:
            diagnostics.prepare_log_diagnostic(prepared(), error=error)
        assert raised.value is original
        state = diagnostics.diagnostic_state_for(error)
        assert state.status is diagnostics.DiagnosticStatus.FALLBACK
        assert diagnostics.record_error(error, stage="outer") is state
        assert await diagnostics.admit_diagnostic(state) is state
    assert len(calls) == 1


async def test_finished_target_reservation_fallback_control_settles_original_waiter():
    failed = asyncio.Event()
    original = asyncio.CancelledError("stderr control")
    serve_control = SystemExit("admission control")

    class ControlledChannel(MemoryChannel):
        async def alog(self, record):
            raise serve_control

    def fallback(message):
        if json.loads(message).get("message") == "reserved":
            raise original
        failed.set()

    with diagnostics.error_diagnostics_context(ControlledChannel(), fallback, background=True):
        state = diagnostics.prepare_log_diagnostic(prepared("reserved"))
        reservation = diagnostics.reserve_diagnostic(state)
        diagnostics.record_error(ValueError("serve failure"), stage="serve")
        await failed.wait()
        waiter = asyncio.create_task(diagnostics.admit_diagnostic(state))
        await asyncio.sleep(0)
        assert not waiter.done()
        try:
            with pytest.raises(asyncio.CancelledError) as raised:
                diagnostics.release_diagnostic(reservation)
            assert raised.value is original
            assert state.status is diagnostics.DiagnosticStatus.FALLBACK
        finally:
            # 即使断言失败也结束本测试自己创建的等待者。
            diagnostics.acknowledge_diagnostic(state)
            await waiter
            with pytest.raises(SystemExit) as stopped:
                await diagnostics.flush_diagnostics()
            assert stopped.value is serve_control


async def test_late_scope_control_still_settles_later_adopted_copy_waiter():
    original = asyncio.CancelledError("first fallback control")
    channel = MemoryChannel()
    calls = []
    entered = asyncio.Event()
    release = asyncio.Event()
    states = []

    def fallback(message):
        text = json.loads(message)["message"]
        calls.append(text)
        if text == "first":
            raise original

    async def late_scope():
        async with diagnostics.diagnostic_scope():
            states.append(diagnostics.prepare_log_diagnostic(prepared("first")))
            states.append(diagnostics.prepare_log_diagnostic(prepared("copy")))
            reservation = diagnostics.reserve_diagnostic(states[1])
            diagnostics.adopt_diagnostic(reservation)
            entered.set()
            await release.wait()

    with diagnostics.error_diagnostics_context(channel, fallback, background=True):
        task = asyncio.create_task(late_scope())
        await entered.wait()
        await diagnostics.flush_diagnostics()
        waiter = asyncio.create_task(diagnostics.admit_diagnostic(states[1]))
        await asyncio.sleep(0)
        assert not waiter.done()
        release.set()
        try:
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
            assert raised.value is original
            assert all(state.status is diagnostics.DiagnosticStatus.FALLBACK for state in states)
            assert await waiter is states[1]
            assert calls == ["first", "copy"]
            assert not channel.records
        finally:
            diagnostics.acknowledge_diagnostic(states[1])
            await waiter


async def test_finished_flush_control_releases_all_reservations_and_finishes_target():
    failed = asyncio.Event()
    original = asyncio.CancelledError("first fallback control")
    serve_control = SystemExit("serve control")
    calls = []

    class ControlledChannel(MemoryChannel):
        async def alog(self, record):
            raise serve_control

    def fallback(message):
        text = json.loads(message).get("message")
        if text is None:
            failed.set()
            return
        calls.append(text)
        if text == "first":
            raise original

    with diagnostics.error_diagnostics_context(ControlledChannel(), fallback, background=True):
        states = [diagnostics.prepare_log_diagnostic(prepared(text)) for text in ("first", "second")]
        for state in states:
            assert diagnostics.reserve_diagnostic(state) is not None
        diagnostics.record_error(ValueError("serve failure"), stage="serve")
        await failed.wait()
        waiters = [asyncio.create_task(diagnostics.admit_diagnostic(state)) for state in states]
        await asyncio.sleep(0)
        try:
            with pytest.raises(asyncio.CancelledError) as raised:
                await diagnostics.flush_diagnostics()
            assert raised.value is original
            assert all(state.status is diagnostics.DiagnosticStatus.FALLBACK for state in states)
            assert await asyncio.gather(*waiters) == states
            assert calls == ["first", "second"]
        finally:
            for state in states:
                diagnostics.acknowledge_diagnostic(state)
            await asyncio.gather(*waiters)
            try:
                await diagnostics.flush_diagnostics()
            except (SystemExit, asyncio.CancelledError):
                pass
