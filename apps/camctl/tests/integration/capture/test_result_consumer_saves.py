"""真实拍摄消费者保存原 RESULTS 事实，并从本地事实继续收尾。"""

from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import (
    advance_winddown,
    capture_handler,
)
from camctl.capture.models import ResultSetPhase, ResultSetSave
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import ControlDriver, DeviceCallResult
from camctl.operations.attempts import (
    AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget, OperationKind, RunFinish, RunOutcome,
)
from camctl.operations.validation import validate_outcome
from camctl.persistence.repositories.operations import OperationRepository
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import register_outputs_guards

from ..support.capture import capture_factory
from ..support.result_cases import CaseClock, ResultPort
from .test_capture_contract import ResultsDouble, _NOW

pytestmark = pytest.mark.asyncio
register_outputs_guards()

from .result_consumer_fixtures import (
    CASES as _CASES, RESULT_EVIDENCE as _EVIDENCE, consumer_world, result_driver, returned,
)


def _actual(activity_id, *, with_files, complete=False):
    entries = [{"identity": "original", "locator": {"path": "/DCIM/original.mp4"},
                "complete": complete, "kind": "video", "size_bytes": 41 if complete else None}]
    return CallOutcome(
        status=AttemptStatus.FAILED,
        error=ErrorValue("transport_timeout", "transport", {"received_bytes": 23}),
        effect=EffectState.CONFIRMED if with_files else EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.ASSUMED,
            EvidenceValue("adb_foreground_assumption", 1, {"terminate_grace_s": Decimal("0.25")})),
        observations=(DeviceObservation("result_files_listed", 1,
            {"activity_id": str(activity_id), "entries": entries}),) if with_files else (),
        call_info=CallInfo(local_exit_code=7),
    )


async def _world(tmp_path, consumer):
    clock = CaseClock(_NOW, 5_000_000_000)
    port = ResultPort(ResultsDouble({}))
    control = create_autospec(ControlDriver, instance=True)
    control.control.return_value = DeviceCallResult.from_outcome(
        returned("operation", _CASES[consumer][3], 1))
    world = await consumer_world(tmp_path, consumer, clock=clock,
        runtime_options={"results": port}, control=control)
    return world, clock, port, control


async def _consume(runtime, consumer, action_id, handler):
    if consumer == "winddown":
        await advance_winddown(action_id, runtime, {}, wait_cap_s=Decimal(0))
    else:
        await capture_handler(handler)(action_id, runtime)


def _assert_actual_saved(owned, action_id, actual, *, attempts_used=1):
    row = owned.connection.execute(
        "SELECT t.status,t.result_json,t.error_json,r.attempts_used FROM operation_attempts t"
        " JOIN operation_runs r ON r.id=t.run_id WHERE r.responsibility_key=? AND t.attempt_no=1",
        (f"results/{action_id}",)).fetchone()
    assert row is not None
    status, result_json, error_json, count = row
    assert (status, count) == (3, attempts_used)
    saved = json.loads(result_json)
    assert saved["settlement"] == {"basis": "assumed", "evidence": {
        "type": "adb_foreground_assumption", "version": 1,
        "data": {"terminate_grace_s": 0.25}}}
    assert saved["call_info"] == {"local_exit": {"exit_code": 7}}
    assert saved["observations"] == [
        {"type": value.type, "version": value.version, "data": dict(value.data)}
        for value in actual.observations]
    assert json.loads(error_json) == {"code": "transport_timeout", "stage": "transport",
                                      "details": {"received_bytes": 23}}


@pytest.mark.parametrize("consumer", tuple(_CASES))
@pytest.mark.parametrize("with_files", [False, True])
async def test_real_result_consumers_save_original_outcome_and_file_input(tmp_path, consumer, with_files):
    (owned, runtime, action_id, handler), clock, port, control = await _world(tmp_path, consumer)
    actual = _actual(action_id, with_files=with_files, complete=consumer == "winddown")
    driver = result_driver(SimpleNamespace(results=port, evidence=_EVIDENCE), actual)
    try:
        await _consume(runtime, consumer, action_id, handler)

        driver.list_results.assert_awaited_once()
        request, _batch = driver.list_results.call_args.args
        assert request.ticket is not None
        assert request.ticket.responsibility_key == f"results/{action_id}"
        assert request.ticket.target_id == str(action_id)
        assert request.timeout_s == Decimal("1.25")
        _assert_actual_saved(owned, action_id, actual)
        if with_files:
            # 完整输入在原观察中保存；不以本用例的 v1 文件观察判定集合结束。
            assert owned.connection.execute(
                "SELECT COUNT(*) FROM device_files WHERE observer_action_id=?"
                " AND json_extract(locator_json,'$.path')='/DCIM/original.mp4'",
                (action_id,)).fetchone() == (1,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("consumer", ["photo", "record", "cancel", "timelapse"])
async def test_closed_result_consumers_use_saved_input_without_device_query(tmp_path, consumer):
    (owned, runtime, action_id, handler), clock, port, control = await _world(tmp_path, consumer)
    check_config = AttemptConfig(3, Decimal("1.25"), Decimal("2"))
    actual = _actual(action_id, with_files=True, complete=True)
    try:
        # 模拟结果已可靠提交、文件登记和动作收尾尚未完成的中断。
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        operations = OperationRepository()
        begun = operations.begin_attempt(AttemptIntent("result", action_id,
            OperationKind.CHECK_CAPTURE_RESULTS, AttemptTarget(activity_id=activity_id),
            None, check_config, clock.wall_us()), new_operation_key(), owned)
        assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
        ticket = begun.value.ticket
        saved = operations.finish_attempt(AttemptFinish(ticket,
            validate_outcome(ticket, actual, _EVIDENCE), clock.wall_us(),
            run_finish=RunFinish(RunOutcome.SUCCEEDED)), new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        if consumer == "timelapse":
            # 集合结论由已保存的独立正式事实提供；v1 观察不提供扫描结束依据。
            receipt = runtime.capture.confirm_result_set(ResultSetSave(
                action_id=action_id, occurred_at=clock.wall_us(),
                phase=ResultSetPhase.UNSATISFIED, contract="task_scope_files",
                observation={"reason": "known_failure"},
                capture={"status": "failed", "error": {"code": "capture_failed", "stage": "execution",
                    "details": {"activity_id": str(action_id), "reason": "invalid_outputs"}}},
                evidence={"method": "known_failure", "observation": {"reason": "known_failure"}},
            ), new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        # 新运行时没有列举缓存，也没有旧结果对象；只能消费持久化原输入。
        resumed_port = ResultPort(ResultsDouble({}))
        factory = await capture_factory(owned, driver=control, evidence=_EVIDENCE,
            results=resumed_port, check_config=check_config, wall_us=clock.wall_us,
            monotonic_ns=lambda: 5_000_000_000)
        resumed = await factory(owned)
        driver = result_driver(SimpleNamespace(results=resumed_port, evidence=_EVIDENCE), actual)
        driver.list_results.side_effect = AssertionError("本地恢复不能再次查询设备")

        await _consume(resumed, consumer, action_id, handler)

        driver.list_results.assert_not_awaited()
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.responsibility_key=?", (f"results/{action_id}",)).fetchone() == (1,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM device_files WHERE observer_action_id=?"
            " AND json_extract(locator_json,'$.path')='/DCIM/original.mp4'",
            (action_id,)).fetchone() == (1,)
        _assert_actual_saved(owned, action_id, actual)
    finally:
        owned.connection.close()
