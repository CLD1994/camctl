"""清理成员与伴随收场原键复验的完整申请核对。

同键同输入重送只读复用、零新增历史；分别改变目标、阶段、时刻、
成功依据、失败详情、取消错误及伴随责任键的增删替换均整组拒绝，
不覆盖既有成员终态、限制或外部调用次数。成员复验按原完整事务边
界核对，不用后来推进的当前阶段替代原阶段。
"""

from __future__ import annotations

import pytest

from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import RunOutcome, StaleRunFinish
from camctl.operations.models import ErrorValue
from camctl.outputs.cleanup_flow import (
    FinishCleanupAction, FinishCleanupItem, CancelCleanupItem,
    ProgressCleanupItem, delete_source_file)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

from ..bootstrap.test_output_binding_changes import _NOW, _save_photos, environment
from .test_cleanup_query_save_recovery import (
    _MatrixDriver, _member_facts, _new_runtime, _start_cleanup)

pytestmark = pytest.mark.asyncio


class _RecordingOutputs(OutputsRepository):
    """记录指定成员事务的申请与键；其余事务真实执行。"""

    def __init__(self, method_name: str):
        super().__init__()
        self.method = method_name
        self.calls = []

    def _record(self, name, request, key, owned):
        method = getattr(super(), name)
        result = method(request, key, owned)
        if result.kind is DbOutcomeKind.COMPLETED:
            self.calls.append((request, key))
        return result

    def finish_cleanup_item(self, request, key, owned):
        return self._record("finish_cleanup_item", request, key, owned)

    def fail_cleanup_item(self, request, key, owned):
        return self._record("fail_cleanup_item", request, key, owned)

    def cancel_cleanup_item(self, request, key, owned):
        return self._record("cancel_cleanup_item", request, key, owned)

    def progress_cleanup_item(self, request, key, owned):
        return self._record("progress_cleanup_item", request, key, owned)


def _history_count(owned) -> int:
    return int(owned.connection.execute(
        "SELECT COUNT(*) FROM history_events").fetchone()[0])


async def _member_output(environment):
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    return cfg, owned, context, output_id


def _expect_rejected(owned, before, invoke):
    """整组拒绝：事务拒绝回滚，历史与成员事实保持。"""
    result = invoke()
    assert result is None or result.kind is not DbOutcomeKind.COMPLETED, result
    assert _history_count(owned) == before


async def test_member_success_reuse_rejects_changed_outcome_and_target(environment):
    """成员成功重送改变依据或目标时整组拒绝。"""
    cfg, owned, context, output_id = await _member_output(environment)
    driver = _MatrixDriver(owned)
    driver.unknown_delete = False
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    recorder = _RecordingOutputs("finish_cleanup_item")
    runtime = _new_runtime(cfg, owned, driver)
    runtime.outputs = recorder
    step = await delete_source_file(runtime.for_item(item_id), item_id)
    assert step.phase == "succeeded", step
    assert len(recorder.calls) == 1
    request, key = recorder.calls[0]
    other_action, other_item = _start_cleanup(owned, output_id, "3")
    before = _history_count(owned)

    _expect_rejected(owned, before, lambda: OutputsRepository().finish_cleanup_item(
        FinishCleanupItem(item_id, _other_outcome(request.outcome),
                          request.occurred_at),
        key, owned))
    _expect_rejected(owned, before, lambda: OutputsRepository().finish_cleanup_item(
        FinishCleanupItem(other_item, request.outcome, request.occurred_at),
        key, owned))
    _expect_rejected(owned, before, lambda: OutputsRepository().finish_cleanup_item(
        FinishCleanupItem(item_id, request.outcome, request.occurred_at + 1),
        key, owned))
    # 同键同输入在其他成员推进后仍只读复用，零新增历史。首个成
    # 员成功后产物已清理：另一成员复用完成事实直接成功。
    other_runtime = _new_runtime(cfg, owned, driver)
    other_step = await delete_source_file(
        other_runtime.for_item(other_item), other_item)
    assert (other_step.phase, other_step.detail) == (
        "succeeded", "ALREADY_CLEANED"), other_step
    finished = OutputsRepository().finish_cleanup_action(
        FinishCleanupAction(other_action, _NOW), new_operation_key(), owned)
    assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
    advanced = _history_count(owned)
    reused = OutputsRepository().finish_cleanup_item(request, key, owned)
    assert reused.kind is DbOutcomeKind.COMPLETED and reused.value.item_id == item_id
    assert _history_count(owned) == advanced


def _other_outcome(outcome):
    from camctl.outputs.cleanup_flow import CleanupOutcomeChoice
    return (CleanupOutcomeChoice.DELETED if outcome is CleanupOutcomeChoice.ABSENCE_CONFIRMED
            else CleanupOutcomeChoice.ABSENCE_CONFIRMED)


async def test_member_failure_reuse_rejects_changed_details(environment):
    """成员失败重送改变完整错误详情时整组拒绝，不只比较错误编号。"""
    cfg, owned, context, output_id = await _member_output(environment)
    driver = _MatrixDriver(owned)
    driver.unknown_delete = True
    driver.matrix_enabled, driver.present = True, None
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    runtime = _new_runtime(cfg, owned, driver, query_attempts=1)
    first = await delete_source_file(runtime, item_id)
    assert first.phase == "query_unknown", first
    recorder = _RecordingOutputs("fail_cleanup_item")
    runtime = _new_runtime(cfg, owned, driver, query_attempts=1)
    runtime.outputs = recorder
    exhausted = await delete_source_file(runtime.for_item(item_id), item_id)
    assert (exhausted.phase, exhausted.detail) == (
        "failed", "file_query_attempts_exhausted"), exhausted
    assert len(recorder.calls) == 1
    request, key = recorder.calls[0]
    before = _history_count(owned)

    from camctl.outputs.cleanup_flow import FailCleanupItem
    changed = FailCleanupItem(
        request.item_id, request.code,
        {**request.details, "attempts_used": request.details["attempts_used"] + 1},
        request.occurred_at)
    _expect_rejected(owned, before, lambda: OutputsRepository().fail_cleanup_item(
        changed, key, owned))
    _expect_rejected(owned, before, lambda: OutputsRepository().fail_cleanup_item(
        FailCleanupItem(request.item_id, "delete_attempts_exhausted",
                        dict(request.details), request.occurred_at), key, owned))
    reused = OutputsRepository().fail_cleanup_item(request, key, owned)
    assert reused.kind is DbOutcomeKind.COMPLETED
    assert _history_count(owned) == before


async def test_progress_reuse_uses_original_boundary_and_rejects_other_input(environment):
    """归属推进复验按原边界核对；不同阶段或不可撤销输入整组拒绝。"""
    cfg, owned, context, output_id = await _member_output(environment)
    driver = _MatrixDriver(owned)
    driver.unknown_delete = False
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    repository = OutputsRepository()
    key = new_operation_key()
    saved = repository.progress_cleanup_item(
        ProgressCleanupItem(item_id, _NOW), key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    before = _history_count(owned)
    facts = _member_facts(owned, item_id)

    # 同键同输入复用；成员后来推进（进入删除中）不改变只读复用。
    runtime = _new_runtime(cfg, owned, driver)
    driver.matrix_enabled, driver.present = True, False
    step = await delete_source_file(runtime.for_item(item_id), item_id)
    assert step.phase == "succeeded", step
    advanced = _history_count(owned)
    reused = repository.progress_cleanup_item(
        ProgressCleanupItem(item_id, _NOW), key, owned)
    assert reused.kind is DbOutcomeKind.COMPLETED and reused.value.item_id == item_id
    assert _history_count(owned) == advanced
    assert facts[:2] == (3, 2)  # 原事务边界：删除中且保持可撤销。


async def test_progress_reuse_rejects_irreversible_on_entry_key(environment):
    """取得归属的原键不能作为不可撤销事实重送。"""
    _, owned, context, output_id = await _member_output(environment)
    driver = _MatrixDriver(owned)
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    repository = OutputsRepository()
    key = new_operation_key()
    saved = repository.progress_cleanup_item(
        ProgressCleanupItem(item_id, _NOW), key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    before = _history_count(owned)

    _expect_rejected(owned, before, lambda: repository.progress_cleanup_item(
        ProgressCleanupItem(item_id, _NOW, irreversible=True), key, owned))
    _expect_rejected(owned, before, lambda: repository.progress_cleanup_item(
        ProgressCleanupItem(item_id, _NOW + 1), key, owned))


async def _dispatched_cancel_member(cfg, owned, output_id, driver):
    """本项派发自己的删除并返回未知，取消生效后保存收场取消。"""
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    driver.unknown_delete = True
    driver.matrix_enabled, driver.present = True, True
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    driver.on_delete_return = lambda: _apply_cleanup_cancel(owned, action_id)
    recorder = _RecordingOutputs("cancel_cleanup_item")
    runtime = _new_runtime(cfg, owned, driver)
    runtime.outputs = recorder
    step = await delete_source_file(runtime.for_item(item_id), item_id)
    assert step.phase == "canceled", step
    assert recorder.calls, "收场取消事务未被记录"
    return action_id, item_id, recorder.calls[-1]


async def test_member_cancel_reuse_keeps_original_when_facts_changed(environment):
    """成员取消复验保持原取消；其他成员后来的完成不改写原结果。"""
    cfg, owned, context, output_id = await _member_output(environment)
    canceled_action, item_id, (request, key) = await _dispatched_cancel_member(
        cfg, owned, output_id, _MatrixDriver(owned))
    before = _history_count(owned)

    # 其他请求后来清理完成同一产物：原取消只读复用，不重新解释。
    other_driver = _MatrixDriver(owned)
    other_driver.unknown_delete = False
    other_action, other_item = _start_cleanup(owned, output_id, "9")
    other_runtime = _new_runtime(cfg, owned, other_driver)
    other_step = await delete_source_file(other_runtime.for_item(other_item), other_item)
    assert other_step.phase == "succeeded", other_step
    changed = _history_count(owned)
    reused = OutputsRepository().cancel_cleanup_item(request, key, owned)
    assert reused.kind is DbOutcomeKind.COMPLETED and reused.value.item_id == item_id
    assert _history_count(owned) == changed
    assert _member_facts(owned, item_id)[:2] == (6, 4)

    # 改变取消错误或时刻均拒绝。
    _expect_rejected(owned, changed, lambda: OutputsRepository().cancel_cleanup_item(
        CancelCleanupItem(item_id, request.occurred_at,
                          "delete_unconfirmed" if request.code == "file_delete_failed"
                          else "file_delete_failed",
                          dict(request.details)), key, owned))
    _expect_rejected(owned, changed, lambda: OutputsRepository().cancel_cleanup_item(
        CancelCleanupItem(item_id, request.occurred_at + 1, request.code,
                          dict(request.details)), key, owned))


async def test_companion_reuse_rejects_changed_responsibility_keys(environment):
    """伴随收场重送改变责任键集合时整组拒绝。"""
    cfg, owned, context, output_id = await _member_output(environment)
    driver = _MatrixDriver(owned)
    driver.unknown_delete = False
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    runtime = _new_runtime(cfg, owned, driver)
    step = await delete_source_file(runtime.for_item(item_id), item_id)
    assert step.phase == "succeeded", step
    operations = OperationRepository()
    request = StaleRunFinish(
        responsibility_keys=(f"delete/{item_id}", f"exists/{item_id}"),
        status=RunOutcome.SUCCEEDED, occurred_at=_NOW)
    key = new_operation_key()
    settled = operations.finish_stale_runs(request, key, owned)
    assert settled.kind is DbOutcomeKind.COMPLETED, settled.error
    before = _history_count(owned)

    # 同键同输入复用（该场景流程已结束，零匹配只读完成）。
    reused = operations.finish_stale_runs(request, key, owned)
    assert reused.kind is DbOutcomeKind.COMPLETED
    assert _history_count(owned) == before

    # 已有伴随事件的场景：在另一产物上构造仍开放的流程并收场。
    other_output = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-b'").fetchone()[0]
    other_driver = _MatrixDriver(owned)
    other_driver.unknown_delete = True
    other_driver.matrix_enabled, other_driver.present = True, None
    other_action, other_item = _start_cleanup(owned, other_output, "4")
    other_runtime = _new_runtime(cfg, owned, other_driver)
    other_step = await delete_source_file(other_runtime.for_item(other_item), other_item)
    assert other_step.phase == "query_unknown", other_step
    open_request = StaleRunFinish(
        responsibility_keys=(f"delete/{other_item}", f"exists/{other_item}"),
        status=RunOutcome.FAILED, occurred_at=_NOW + 10,
        error=ErrorValue("delete_attempts_exhausted", "delete"))
    open_key = new_operation_key()
    settled = operations.finish_stale_runs(open_request, open_key, owned)
    assert settled.kind is DbOutcomeKind.COMPLETED, settled.error
    after = _history_count(owned)
    assert settled.value.finished_run_ids

    # 子集、替换、增加键都拒绝；不改写已收场流程。
    for keys in (
        (f"delete/{other_item}",),
        (f"delete/{item_id}", f"exists/{other_item}"),
        (f"delete/{other_item}", f"exists/{other_item}", f"delete/{item_id}"),
    ):
        mutated = StaleRunFinish(
            responsibility_keys=keys, status=RunOutcome.FAILED,
            occurred_at=_NOW + 10,
            error=ErrorValue("delete_attempts_exhausted", "delete"))
        _expect_rejected(owned, after, lambda mutated=mutated:
                         operations.finish_stale_runs(mutated, open_key, owned))
    # 改变结果分类或错误也拒绝。
    for status, error in (
        (RunOutcome.CANCELED, None),
        (RunOutcome.FAILED, ErrorValue("device_binding_unavailable", "execution")),
    ):
        mutated = StaleRunFinish(
            responsibility_keys=open_request.responsibility_keys,
            status=status, occurred_at=_NOW + 10, error=error)
        _expect_rejected(owned, after, lambda mutated=mutated:
                         operations.finish_stale_runs(mutated, open_key, owned))
    # 原输入重送仍只读复用。
    reused = operations.finish_stale_runs(open_request, open_key, owned)
    assert reused.kind is DbOutcomeKind.COMPLETED
    assert reused.value.finished_run_ids == settled.value.finished_run_ids
    assert _history_count(owned) == after


async def test_companion_scope_covers_open_ended_and_unknown_keys(environment):
    """伴随申请可以包含已结束与不存在的责任键；只结束开放流程。"""
    cfg, owned, context, output_id = await _member_output(environment)
    driver = _MatrixDriver(owned)
    driver.unknown_delete = True
    driver.matrix_enabled, driver.present = True, None
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    runtime = _new_runtime(cfg, owned, driver, query_attempts=1)
    first = await delete_source_file(runtime, item_id)
    assert first.phase == "query_unknown", first
    operations = OperationRepository()
    # 查询流程已由结果关闭重试等待但仍开放；删除流程开放。
    request = StaleRunFinish(
        responsibility_keys=(
            f"delete/{item_id}", f"exists/{item_id}", f"delete/{item_id + 999}"),
        status=RunOutcome.FAILED, occurred_at=_NOW + 20,
        error=ErrorValue("file_query_attempts_exhausted", "query"))
    key = new_operation_key()
    settled = operations.finish_stale_runs(request, key, owned)
    assert settled.kind is DbOutcomeKind.COMPLETED, settled.error
    assert len(settled.value.finished_run_ids) == 2  # 不存在的键不产生事件。
    before = _history_count(owned)
    reused = operations.finish_stale_runs(request, key, owned)
    assert reused.kind is DbOutcomeKind.COMPLETED
    assert reused.value.finished_run_ids == settled.value.finished_run_ids
    assert _history_count(owned) == before
