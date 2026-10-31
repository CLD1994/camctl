"""延时处理器异步查询归属时，其他设备可以增删自己的扫描。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import CaptureRuntime, capture_handler
from camctl.capture.result_pages import ResultPageRef
from camctl.capture.result_scans import PendingResultScan
from camctl.contracts.values import ConsistencyError
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from support.capture_database import capture_database

from .test_result_page_saves import _REGISTRY, _request
from .test_result_pages import _CURSOR, _TICKET

pytestmark = pytest.mark.asyncio


def _runtime(*, own_page):
    runtime = create_autospec(CaptureRuntime, instance=True)
    runtime.database = capture_database()
    runtime.capture = create_autospec(CaptureRepository, instance=True)
    runtime.natural_wait_states = {}
    runtime.timelapse_deadlines = {}
    runtime.action.return_value = {"status": 3}
    own = PendingResultScan(_TICKET, None, Decimal("10"))
    other = PendingResultScan(replace(_TICKET, run_id=10, target_id="72",
        responsibility_key="results/72"), None, Decimal("10"))
    request = _request()
    actual = request.outcome.outcome
    actual = replace(actual, observations=tuple(replace(observation,
        data={**observation.data, "activity_id": other.ticket.target_id})
        for observation in actual.observations))
    other.saves.begin(replace(request, ticket=other.ticket,
        outcome=validate_outcome(other.ticket, actual, _REGISTRY)))
    runtime.pending_result_scans = {
        (other.ticket.run_id, other.ticket.attempt_id): other,
        (own.ticket.run_id, own.ticket.attempt_id): own,
    }
    response = ResultPageRef(_TICKET, 1, 10, _CURSOR)
    runtime.capture.save_result_page.return_value = DbOutcome(DbOutcomeKind.COMPLETED, response)
    pending = own.saves.begin(_request()) if own_page else None
    return runtime, own, other, pending, response


@pytest.mark.parametrize("mutation", ["add", "remove"])
@pytest.mark.parametrize("own_page", [False, True], ids=["waiting", "original-page"])
async def test_other_device_scan_changes_during_lookup_keep_original_page_owner(mutation, own_page):
    runtime, own, other, pending, response = _runtime(own_page=own_page)
    entered, release = asyncio.Event(), asyncio.Event()

    async def action_id(ticket):
        if ticket.run_id == other.ticket.run_id:
            entered.set()
            await release.wait()
            return 22
        return 21

    runtime.action_id_of_ticket.side_effect = action_id
    task = asyncio.create_task(capture_handler("camera_timelapse")(21, runtime))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        if mutation == "remove":
            runtime.pending_result_scans.pop((other.ticket.run_id, other.ticket.attempt_id))
        else:
            added = PendingResultScan(replace(_TICKET, run_id=11), None, Decimal("10"))
            runtime.pending_result_scans[(added.ticket.run_id, added.ticket.attempt_id)] = added
        release.set()
        assert await asyncio.wait_for(task, timeout=1) is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert runtime.pending_result_scans[(own.ticket.run_id, own.ticket.attempt_id)] is own
    assert other.saves.pending.response is None
    if mutation == "add":
        assert runtime.pending_result_scans[(added.ticket.run_id, added.ticket.attempt_id)] is added
        # 下一次推进取得更新后的清单，但仍只消费当前动作的原申请。
        runtime.action_id_of_ticket.reset_mock()
        runtime.action_id_of_ticket.side_effect = lambda ticket: 21 if ticket == own.ticket else 22
        await capture_handler("camera_timelapse")(21, runtime)
        runtime.action_id_of_ticket.assert_any_await(added.ticket)
    else:
        assert (other.ticket.run_id, other.ticket.attempt_id) not in runtime.pending_result_scans
    if own_page:
        runtime.capture.save_result_page.assert_called_once()
        request, key, _ = runtime.capture.save_result_page.call_args.args
        assert request == pending.request and key == pending.key
        assert own.saves.pending.response is response
    else:
        runtime.capture.save_result_page.assert_not_called()


@pytest.mark.parametrize("stage", ["lookup", "save"])
async def test_scan_recovery_preserves_original_lookup_or_save_exception(stage):
    runtime, own, _, pending, _ = _runtime(own_page=True)
    failure = RuntimeError("原查询或保存错误")
    if stage == "lookup":
        runtime.action_id_of_ticket.side_effect = failure
    else:
        runtime.action_id_of_ticket.side_effect = lambda ticket: 21 if ticket == own.ticket else 22
        runtime.capture.save_result_page.side_effect = failure

    with pytest.raises(RuntimeError if stage == "lookup" else ConsistencyError) as raised:
        await capture_handler("camera_timelapse")(21, runtime)

    if stage == "lookup":
        assert raised.value is failure
    else:
        assert raised.value.__cause__ is failure
    assert own.saves.pending.request == pending.request
    assert own.saves.pending.key == pending.key
    assert own.saves.pending.response is None
