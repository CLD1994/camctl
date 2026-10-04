"""C8 取消后处理收场编排的单元测试。

取消决定已可靠保存（discard PENDING）后：先保存运行阶段再按动作
归属清理中间文件，全部非失败出口保存完成，任一文件清理失败保存
失败终态与错误结构；执行中恢复入口沿用既有进度，终态重复进入不
再保存。
"""

from __future__ import annotations

import pytest

from camctl.capture.media import (
    DiscardContext,
    DiscardExecutionPhase,
    SaveDisposition,
    SaveReceipt,
    execute_discard,
)
from camctl.capture.processing import DiscardPhase, DiscardProgressSave
from camctl.outputs.work_files import (
    WorkFileAction,
    WorkFileDecision,
    WorkFileSingleOutcome,
    WorkFileSingleResult,
    WorkFileCleanupError,
)

_NOW = 1_750_000_000_000_000


def _saved() -> SaveReceipt:
    return SaveReceipt(SaveDisposition.SAVED)


def _receipt(disposition: SaveDisposition) -> SaveReceipt:
    return SaveReceipt(disposition, error=RuntimeError("db"))


class _Saves:
    """按脚本回执的保存替身；记录调用。"""

    def __init__(self, script: dict | None = None) -> None:
        self.script = script or {}
        self.calls: list[DiscardProgressSave] = []

    def save_discard_progress(self, command) -> SaveReceipt:
        self.calls.append(command)
        receipt = self.script.get("save_discard_progress", _saved())
        return receipt(command) if callable(receipt) else receipt


class _Cleaning:
    """清理端口替身：返回脚本结果并记录调用。"""

    def __init__(self, files=(701, 702), outcomes=None, error=None) -> None:
        self.files = tuple(files)
        self.outcomes = dict(outcomes or {})
        self.error = error
        self.cleaned: list[int] = []

    def action_work_files(self, action_id: int) -> tuple[int, ...]:
        return self.files

    async def clean(self, file_id: int) -> WorkFileSingleResult:
        self.cleaned.append(file_id)
        if self.error is not None:
            raise self.error
        outcome = self.outcomes.get(file_id, WorkFileSingleOutcome.DELETED)
        return WorkFileSingleResult(
            file_id=file_id,
            decision=WorkFileDecision(
                WorkFileAction.DELETE, "归属终态且操作停止"),
            outcome=outcome)


def _context(saves: _Saves, cleaning: _Cleaning, *,
             discard_state: int = 2) -> DiscardContext:
    return DiscardContext(
        processing_id=1, action_id=1, discard_state=discard_state,
        saves=saves, cleaning=cleaning, occurred_at=_NOW)


@pytest.mark.asyncio
async def test_discard_runs_then_cleans_and_completes() -> None:
    saves = _Saves()
    cleaning = _Cleaning()
    step = await execute_discard(_context(saves, cleaning))
    assert step.phase is DiscardExecutionPhase.CLEANED
    assert [command.phase for command in saves.calls] == [
        DiscardPhase.RUNNING, DiscardPhase.COMPLETED]
    assert cleaning.cleaned == [701, 702]
    assert (step.cleaned, step.failed) == (2, 0)


@pytest.mark.asyncio
async def test_discard_without_files_completes() -> None:
    saves = _Saves()
    step = await execute_discard(_context(saves, _Cleaning(files=())))
    assert step.phase is DiscardExecutionPhase.CLEANED
    assert [command.phase for command in saves.calls] == [
        DiscardPhase.RUNNING, DiscardPhase.COMPLETED]


@pytest.mark.asyncio
async def test_discard_resumes_from_running_without_restart() -> None:
    saves = _Saves()
    step = await execute_discard(
        _context(saves, _Cleaning(), discard_state=3))
    assert step.phase is DiscardExecutionPhase.CLEANED
    assert [command.phase for command in saves.calls] == [
        DiscardPhase.COMPLETED]


@pytest.mark.asyncio
@pytest.mark.parametrize("state, phase", [
    (4, DiscardExecutionPhase.ALREADY_FINISHED),
    (5, DiscardExecutionPhase.ALREADY_FINISHED),
    (6, DiscardExecutionPhase.ALREADY_FINISHED),
    (1, DiscardExecutionPhase.NOT_PENDING),
])
async def test_discard_entry_states(state, phase) -> None:
    saves = _Saves()
    step = await execute_discard(
        _context(saves, _Cleaning(), discard_state=state))
    assert step.phase is phase
    assert saves.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition, phase", [
    (SaveDisposition.REJECTED, DiscardExecutionPhase.START_REJECTED),
    (SaveDisposition.UNKNOWN, DiscardExecutionPhase.START_UNKNOWN),
])
async def test_discard_does_not_clean_before_saved_running(disposition, phase) -> None:
    saves = _Saves({"save_discard_progress": _receipt(disposition)})
    cleaning = _Cleaning()
    step = await execute_discard(_context(saves, cleaning))
    assert step.phase is phase
    assert cleaning.cleaned == []


@pytest.mark.asyncio
async def test_failed_file_cleanup_saves_failure() -> None:
    saves = _Saves()
    cleaning = _Cleaning(outcomes={701: WorkFileSingleOutcome.FAILED})
    step = await execute_discard(_context(saves, cleaning))
    assert step.phase is DiscardExecutionPhase.CLEANUP_FAILED
    assert cleaning.cleaned == [701, 702]
    assert [command.phase for command in saves.calls] == [
        DiscardPhase.RUNNING, DiscardPhase.FAILED]
    failure = saves.calls[1]
    assert failure.error is not None
    assert failure.error.code == "action_work_file_delete_failed"
    assert failure.error.stage == "discard"
    assert failure.error.details["file_id"] == "701"
    assert (step.cleaned, step.failed) == (1, 1)


@pytest.mark.asyncio
async def test_cleanup_port_error_saves_failure() -> None:
    saves = _Saves()
    cleaning = _Cleaning(error=WorkFileCleanupError("retention_release_failed", "db"))
    step = await execute_discard(_context(saves, cleaning))
    assert step.phase is DiscardExecutionPhase.CLEANUP_FAILED
    failure = saves.calls[1]
    assert failure.error is not None
    assert failure.error.code == "cleanup_failed"
    assert failure.error.details["stage"] == "retention_release_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition, phase", [
    (SaveDisposition.REJECTED, DiscardExecutionPhase.RESULT_REJECTED),
    (SaveDisposition.UNKNOWN, DiscardExecutionPhase.RESULT_UNKNOWN),
])
async def test_discard_result_save_outcome_is_reported(disposition, phase) -> None:
    receipts = [_saved(), _receipt(disposition)]

    def progress(command):
        return receipts.pop(0)

    saves = _Saves({"save_discard_progress": progress})
    step = await execute_discard(_context(saves, _Cleaning()))
    assert step.phase is phase
    assert len(saves.calls) == 2
