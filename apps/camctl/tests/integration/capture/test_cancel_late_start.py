"""取消生效后迟到启动结果的有限停止与重入。"""

from decimal import Decimal

import pytest

from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig, RunOutcome
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository, register_cancellation_guards

from .test_capture_contract import (
    _NOW, _environment, _runtime, _value, StopDouble,
)

register_cancellation_guards()
pytestmark = pytest.mark.asyncio


def _cancel(owned, action_id, *, origin_id=50, mode=CancelApplyMode.WITH_STOP, occurred_at=_NOW):
    from camctl.persistence.transaction import row_facts

    origin = row_facts(owned.connection, "actions", action_id)
    origin.update(id=origin_id, name=f"cancel-{origin_id}", input_index=origin_id, type=6, device_id=None,
                  driver_id=None, effective_params_json=None, max_delay_ms=None,
                  execution_spec_json="{}", input_fields_json="{}", target_selection_state=2)
    owned.connection.execute(
        f"INSERT INTO actions ({','.join(origin)}) VALUES ({','.join('?' for _ in origin)})",
        tuple(origin.values()))
    owned.connection.execute(
        "INSERT INTO cancel_items (id, action_id, target_action_id, selection_basis,"
        " status, cancellation_effect) VALUES (?, ?, ?, 1, 1, 1)", (origin_id + 41, origin_id, action_id))
    result = CancellationRepository().apply_cancel_target(
        ApplyCancelTarget(origin_id + 41, mode, occurred_at), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error


class StopResult:
    def __init__(self, action_id, success):
        self.action_id, self.success, self.calls = action_id, success, 0

    async def stop(self, request):
        self.calls += 1
        if self.success:
            result = await StopDouble(str(self.action_id)).stop(request)
            if self.success == "confirmed_error":
                return DeviceCallResult(observations=result.observations,
                                        error={"code": "transport_timeout", "message": "timeout"})
            return result
        return DeviceCallResult(observations=(), error={"code": "transport_timeout", "message": "timeout"})


@pytest.mark.parametrize("action_id,action_type,handler", [
    (12, 2, "camera_record"), (13, 3, "camera_timelapse"), (12, 2, "winddown")])
@pytest.mark.parametrize("sent", [False, True])
@pytest.mark.parametrize("late_error", [False, True])
@pytest.mark.parametrize("stop_success", [False, True, "confirmed_error"])
async def test_cancel_late_unknown_reuses_stop_and_finishes(
        tmp_path, action_id, action_type, handler, sent, late_error, stop_success):
    owned = _environment(tmp_path, ((action_id, action_type),))
    try:
        async def advance(runtime):
            if handler == "winddown":
                from camctl.capture.handlers import advance_winddown
                await advance_winddown(action_id, runtime, {}, wait_cap_s=Decimal("1"))
            else:
                await capture_handler(handler)(action_id, runtime)

        config = AttemptConfig(1, Decimal("10"), Decimal("1"))
        runtime = _runtime(owned, stop_config=config)
        ticket, reason = runtime.grant(runtime.action(action_id))
        assert ticket is not None, reason
        _cancel(owned, action_id)
        _cancel(owned, action_id, origin_id=60, mode=CancelApplyMode.ALREADY)
        stop = StopResult(action_id, stop_success)
        runtime.stopper = stop
        # 启动仍在途时，取消不能提前假定效果或派发新的停止。
        await advance(runtime)
        assert stop.calls == 0
        assert _value(owned, "SELECT status FROM actions WHERE id = ?", action_id) == (2,)
        runtime.finish(ticket, CallOutcome(
            status=AttemptStatus.FAILED if late_error else AttemptStatus.UNKNOWN,
            error=ErrorValue("transport_timeout", "transport"), effect=EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("operation_returned", 1, {})),
            observations=()), end_run=RunOutcome.FAILED if late_error else RunOutcome.UNCONFIRMED,
            run_error=ErrorValue("result_unconfirmed", "device"))
        if sent:
            owned.connection.execute("UPDATE device_activities SET sent_at = ? WHERE action_id = ?", (_NOW, action_id))
        # 新运行时不继承计时锚点，沿原 START/STOP 事实恢复。
        runtime = _runtime(owned, stop_config=config)
        runtime.stopper = stop
        await advance(runtime)
        if stop_success == "confirmed_error":
            await advance(runtime)
        if not stop_success:
            await advance(runtime)
            assert stop.calls == 1  # 重试间隔及预算不允许新增调用。
            runtime.monotonic_ns = lambda: 7_000_000_000
            await advance(runtime)
        assert _value(owned, "SELECT status, cancel_requested, execution_started FROM actions WHERE id = ?", action_id) == (6, 1, 1)
        assert _value(owned, "SELECT status, attempts_used, retry_wait_required FROM operation_runs WHERE responsibility_key = ?", f"start/{action_id}") == (5, 1, 0)
        assert _value(owned, "SELECT status, attempts_used, retry_wait_required FROM operation_runs WHERE responsibility_key = ?", f"stop/{action_id}") == ((3 if stop_success else 6), 1, 0)
        assert _value(owned, "SELECT activity_state, occupancy_state, started_at FROM device_activities WHERE action_id = ?", action_id) == ((3 if stop_success else 1), (2 if stop_success else 1), None)
        before = tuple(owned.connection.iterdump())
        await advance(runtime)
        assert tuple(owned.connection.iterdump()) == before
        assert stop.calls == 1
        # 目标 CANCELED 与本次取消是否成功是两个结论，停止未知不能报成功。
        from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
        from camctl.cancellation.settlement import TargetSettlement
        from camctl.cancellation.models import FinishCancelAction

        cancellations = CancellationRepository()
        settlement = TargetSettlement(owned, None, cancellations, lambda _: "unknown", lambda: _NOW)
        outcome = await settlement.settle(action_id)
        assert outcome.complete and outcome.failed is (not stop_success)
        progress = await apply_cancel(ApplyCancel(50, (91,)), CancellationRuntime(
            owned, cancellations, settlement, lambda: _NOW))
        assert progress.items[0].status == (3 if stop_success else 4)
        from camctl.contracts.workflow_errors import item_error_id, registered_error
        assert progress.items[0].error_code == (None if stop_success else
                                                item_error_id("cancel_items", "target_cleanup_failed"))
        finished = cancellations.finish_cancel_action(FinishCancelAction(50, _NOW), new_operation_key(), owned)
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        assert _value(owned, "SELECT status, error_code FROM actions WHERE id = 50") == (
            3 if stop_success else 4,
            None if stop_success else registered_error("cancel_items_failed")["action_error_id"])
        saved = tuple(owned.connection.iterdump())
        await apply_cancel(ApplyCancel(50, (91,)), CancellationRuntime(
            owned, cancellations, settlement, lambda: _NOW))
        assert tuple(owned.connection.iterdump()) == saved
    finally:
        owned.connection.close()
