"""清理查询恢复先保存全部原结果，并以实际成员终态收场。"""

from __future__ import annotations

from sqlite3 import OperationalError

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.attempts import RunOutcome
from camctl.outputs.cleanup_flow import (
    CleanupOutcomeChoice, FinishCleanupItem, _verify_before_delete,
    advance_cleanup, delete_source_file,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.cancellation import register_cancellation_guards
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import OperationRepository, register_operation_guards
from camctl.persistence.repositories.outputs import OutputsRepository, register_outputs_guards

from .test_cleanup_query_local_consumption import (
    _assert_history_prefix, _first_rejected, _fresh_runtime, _history,
    _instrument, _query_facts, _saved_key_count, _world,
)
from .test_cleanup_query_save_recovery import _member_facts, _start_cleanup
from ..bootstrap.test_output_binding_changes import _NOW, environment


pytestmark = pytest.mark.asyncio

register_operation_guards()
register_outputs_guards()
register_capture_guards()
register_cancellation_guards()


class _BlockedQueryResults(OperationRepository):
    """真实仓储保存未阻塞的结果；阻塞结果符合 ROLLED_BACK 端口契约。"""

    def __init__(self, blocked):
        self.blocked = set(blocked)
        self.calls = []

    def finish_attempt(self, request, key, owned):
        if request.ticket.operation == "query":
            self.calls.append((request, key))
            if int(request.ticket.target_id) in self.blocked:
                return DbOutcome(
                    kind=DbOutcomeKind.ROLLED_BACK,
                    error=OperationalError("原查询结果尚未保存"),
                )
        return super().finish_attempt(request, key, owned)


async def test_advance_saves_all_original_queries_before_any_new_device_call(environment):
    # A 接手 C1 的未知删除，自己的原查询属于 BEFORE_DELETE。
    base, driver, action_a, item_a, _ = await _world(environment, "before", True)
    owned = base.owned
    output_a = owned.connection.execute(
        "SELECT output_id FROM cleanup_items WHERE id=?", (item_a,),
    ).fetchone()[0]
    output_b = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-b'",
    ).fetchone()[0]
    _, item_b = _start_cleanup(owned, output_b, "5")
    assert item_a < item_b
    port = _BlockedQueryResults((item_a, item_b))
    base.operations = port

    with pytest.raises(ConsistencyError):
        await _verify_before_delete(base.for_item(item_a), item_a, action_a, output_a)
    with pytest.raises(ConsistencyError):
        await delete_source_file(base.for_item(item_b), item_b)
    originals = {int(request.ticket.target_id): (request, key)
                 for request, key in port.calls}
    assert set(originals) == {item_a, item_b}
    assert _query_facts(owned, item_a)[0][1] == 1
    assert _query_facts(owned, item_b)[0][1] == 1
    assert _member_facts(owned, item_a)[0] == 3
    assert _member_facts(owned, item_b)[0] == 3
    calls_before = (len(driver.deletes), len(driver.queries))

    # A 原结果可以可靠提交，B 仍拒绝。A 的在场观察不能抢先触发删除。
    port.blocked.remove(item_a)
    with pytest.raises(ConsistencyError):
        await advance_cleanup(base)

    assert (len(driver.deletes), len(driver.queries)) == calls_before
    assert _query_facts(owned, item_a)[0][1] == 1
    assert _query_facts(owned, item_b)[0][1] == 1
    for item_id in (item_a, item_b):
        retries = [(request, key) for request, key in port.calls
                   if int(request.ticket.target_id) == item_id]
        assert len(retries) == 2
        assert retries[1][0] is originals[item_id][0]
        assert retries[1][1] == originals[item_id][1]
    for item_id, saved in ((item_a, True), (item_b, False)):
        original_run = originals[item_id][0].ticket.run_id
        result_json = owned.connection.execute(
            "SELECT result_json FROM operation_attempts WHERE run_id=?",
            (original_run,),
        ).fetchone()[0]
        assert (result_json is not None) is saved
    assert _member_facts(owned, item_a)[0] == 3
    assert _member_facts(owned, item_b)[0] == 3


async def test_other_success_key_does_not_become_original_member_save(environment):
    base, driver, _, item_id, _ = await _world(environment, "after", False)
    runtime, members, operations = _instrument(
        base, item_id, member_method="finish_cleanup_item", after_commit=False)
    await _first_rejected(runtime, item_id, {"result_rejected"})
    assert members.fault is not None and members.fault.triggered
    assert members.results[0].kind is DbOutcomeKind.UNKNOWN
    _, _, original_key = members.calls[0]
    original_query = operations.query_facts
    calls_before = (len(driver.deletes), len(driver.queries))
    runtime.owned.connection.close()

    fresh = environment[2].open_connection()
    try:
        assert _saved_key_count(fresh, original_key) == 0
        assert _member_facts(fresh, item_id)[0] == 3
        # 同一可靠缺席事实允许另一公开申请先保存成功；旧申请并未提交。
        actual_key = new_operation_key()
        actual = OutputsRepository().finish_cleanup_item(
            FinishCleanupItem(
                item_id, CleanupOutcomeChoice.ABSENCE_CONFIRMED, _NOW + 100),
            actual_key, fresh)
        assert actual.kind is DbOutcomeKind.COMPLETED, actual.error
        actual_member = _member_facts(fresh, item_id)
        assert actual_member[:4] == (4, 4, 3, None)
        assert actual_member[4] is not None
        assert _saved_key_count(fresh, actual_key) == 1
        before_recovery = _history(fresh)
        resumed = _fresh_runtime(runtime, fresh, driver.query_requests[-1].binding)

        step = await delete_source_file(resumed, item_id)

        assert step.phase in {"succeeded", "already_terminal"}, step
        assert _member_facts(fresh, item_id) == actual_member
        assert _query_facts(fresh, item_id) == original_query
        assert _saved_key_count(fresh, actual_key) == 1
        assert _saved_key_count(fresh, original_key) == 0
        assert (len(driver.deletes), len(driver.queries)) == calls_before
        _assert_history_prefix(fresh, before_recovery)
        assert len(operations.companion_calls) == 1
        companion, _ = operations.companion_calls[0]
        assert companion.status is RunOutcome.SUCCEEDED
        assert companion.error is None
        run_status = int(enum_for("operation_runs.status").SUCCEEDED)
        assert fresh.connection.execute(
            "SELECT status,error_json FROM operation_runs WHERE cleanup_item_id=? ORDER BY id",
            (item_id,),
        ).fetchall() == [(run_status, None), (run_status, None)]
    finally:
        fresh.connection.close()
