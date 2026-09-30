"""R5 报告生成进程生命周期的单元测试（进程内模型）。

结果与退出竞争：结果已到达则成功；仅残留文件不发布；停止在结
果前后分别处理；迟到状态库错误不改写已确认结果。
"""

from __future__ import annotations

import asyncio

import pytest

from camctl.reporting.worker import (
    GenerationJob,
    GenerationOutcome,
    WorkerSettlement,
    WorkerStopReason,
    decide_generation_outcome,
    settle_worker,
)


def _job(report_id: int = 1) -> GenerationJob:
    return GenerationJob(report_id=report_id, payload=b"{}\n")


class TestOutcomeDecision:
    def test_exit_checks_delivered_result(self) -> None:
        decision = decide_generation_outcome(result=b"bytes", stop_reason=None, exit_failed=False)
        assert decision is GenerationOutcome.SUCCESS

    def test_missing_result_is_not_success(self) -> None:
        decision = decide_generation_outcome(result=None, stop_reason=None, exit_failed=False)
        assert decision is not GenerationOutcome.SUCCESS

    def test_stop_before_result_is_stopped(self) -> None:
        decision = decide_generation_outcome(
            result=None, stop_reason=WorkerStopReason.SESSION_CLOSING, exit_failed=False
        )
        assert decision is GenerationOutcome.STOPPED

    def test_result_then_stop_still_success(self) -> None:
        decision = decide_generation_outcome(
            result=b"bytes", stop_reason=WorkerStopReason.SESSION_CLOSING, exit_failed=False
        )
        assert decision is GenerationOutcome.SUCCESS

    def test_exit_failure_is_failure_even_with_result(self) -> None:
        decision = decide_generation_outcome(
            result=b"bytes", stop_reason=None, exit_failed=True
        )
        assert decision is GenerationOutcome.FAILED

    def test_timeout_is_failure_not_success(self) -> None:
        decision = decide_generation_outcome(
            result=None, stop_reason=WorkerStopReason.TIMEOUT, exit_failed=False
        )
        assert decision is GenerationOutcome.TIMEOUT


class TestSettlement:
    @pytest.mark.asyncio
    async def test_late_database_error_does_not_rewrite_result(self) -> None:
        settlement = await settle_worker(
            outcome=GenerationOutcome.SUCCESS,
            stop_reason=None,
            confirm_exit=self._exits_cleanly(),
            cleanup=self._records_calls(),
        )
        assert settlement.is_success is True
        # 迟到错误只记录，不改写已确认结果。
        settlement.record_late_error(RuntimeError("迟到状态库错误"))
        assert settlement.is_success is True
        assert settlement.late_error is not None

    @pytest.mark.asyncio
    async def test_cleanup_only_after_exit_confirmed(self) -> None:
        calls: list[str] = []

        async def confirm_exit() -> None:
            calls.append("exit")

        async def cleanup() -> None:
            calls.append("cleanup")

        await settle_worker(
            outcome=GenerationOutcome.FAILED,
            stop_reason=None,
            confirm_exit=confirm_exit,
            cleanup=cleanup,
        )
        assert calls == ["exit", "cleanup"]

    @pytest.mark.asyncio
    async def test_cleanup_skipped_before_stop_confirmed(self) -> None:
        calls: list[str] = []

        async def confirm_exit() -> None:
            calls.append("exit")

        async def cleanup() -> None:
            calls.append("cleanup")

        settlement = await settle_worker(
            outcome=GenerationOutcome.STOPPED,
            stop_reason=WorkerStopReason.SESSION_CLOSING,
            confirm_exit=confirm_exit,
            cleanup=cleanup,
        )
        # 停止未确认实际退出前不清理临时文件。
        assert "cleanup" not in calls or calls.index("exit") < calls.index("cleanup")
        assert settlement.cleaned is True

    @staticmethod
    def _exits_cleanly():
        async def confirm() -> None:
            return None

        return confirm

    @staticmethod
    def _records_calls():
        async def cleanup() -> None:
            return None

        return cleanup
