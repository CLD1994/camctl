"""C4 单张拍摄独立流程的单元测试。

照片按自身完成声明分别执行：完成后返回契约使用响应完成证据，
只发送契约等待并核实产物；单张任务不默认录像计时或停止；未启动
取消不创建尝试，可能启动且无停止能力时不以停止覆盖未知效果，
取消后到达的合法完成文件保留。
"""

from __future__ import annotations

import pytest

from camctl.capture.photo import (
    PhotoCompletion,
    PhotoDecision,
    PhotoDispatch,
    PhotoState,
    decide_photo,
    run_photo,
)

pytestmark = pytest.mark.asyncio


def _state(**overrides) -> PhotoState:
    values = dict(
        action_terminal=False,
        canceled=False,
        dispatched=True,
        response_completed=False,
        response_failed=False,
        effect_unknown=False,
        stop_supported=True,
    )
    values.update(overrides)
    return PhotoState(**values)


def _assessment(
    *, complete=False, explicitly_unmet=False, read_error=False
):
    from camctl.capture.photo import CaptureAssessment

    return CaptureAssessment(
        complete=complete,
        explicitly_unmet=explicitly_unmet,
        read_error=read_error,
    )


class TestDeclaredCompletion:
    @pytest.mark.parametrize("response_completed,explicitly_unmet,read_error,expected", [
        (True, True, False, PhotoDecision.FAILED_KEEP_FILES),
        (True, False, False, PhotoDecision.VERIFY_RESULTS),
        (True, False, True, PhotoDecision.VERIFY_RESULTS),
        (True, True, True, PhotoDecision.VERIFY_RESULTS),
        (False, True, False, PhotoDecision.WAIT_RESPONSE),
    ])
    async def test_output_failure_requires_actual_completion(
        self, response_completed, explicitly_unmet, read_error, expected):
        assert decide_photo(_state(response_completed=response_completed),
            _assessment(explicitly_unmet=explicitly_unmet, read_error=read_error),
            PhotoCompletion.COMPLETED_ON_RETURN) is expected

    @pytest.mark.parametrize("response_completed,complete,completion,expected", [
        (True, False, PhotoCompletion.COMPLETED_ON_RETURN, PhotoDecision.VERIFY_RESULTS),
        (False, True, PhotoCompletion.SENT_ONLY, PhotoDecision.REGISTER_SUCCESS),
        (False, False, PhotoCompletion.SENT_ONLY, PhotoDecision.VERIFY_RESULTS),
    ])
    async def test_photo_uses_declared_completion(
            self, response_completed, complete, completion, expected) -> None:
        """完成响应与文件依据分别满足所属契约。"""
        decision = decide_photo(
            _state(response_completed=response_completed),
            _assessment(complete=complete),
            completion,
        )
        assert decision is expected

    async def test_complete_response_and_complete_files_allow_success(self) -> None:
        decision = decide_photo(
            _state(response_completed=True),
            _assessment(complete=True),
            PhotoCompletion.COMPLETED_ON_RETURN,
        )
        assert decision is PhotoDecision.REGISTER_SUCCESS

    async def test_completion_evidence_is_not_invented(self) -> None:
        """完成后返回契约未取得完成响应：继续等待，不因文件齐而成功。"""
        decision = decide_photo(
            _state(response_completed=False),
            _assessment(complete=True),
            PhotoCompletion.COMPLETED_ON_RETURN,
        )
        assert decision is PhotoDecision.WAIT_RESPONSE


class TestCancelAndFailure:
    async def test_canceled_before_dispatch_creates_no_attempt(self) -> None:
        decision = decide_photo(
            _state(canceled=True, dispatched=False),
            _assessment(),
            PhotoCompletion.SENT_ONLY,
        )
        assert decision is PhotoDecision.NOT_DISPATCHED_CANCELED

    async def test_explicit_failure_keeps_files(self) -> None:
        decision = decide_photo(
            _state(response_failed=True),
            _assessment(complete=True),
            PhotoCompletion.COMPLETED_ON_RETURN,
        )
        assert decision is PhotoDecision.FAILED_KEEP_FILES

    async def test_unknown_without_stop_is_not_stopped(self) -> None:
        """可能启动且设备无停止能力：保留未知，不以停止覆盖。"""
        decision = decide_photo(
            _state(effect_unknown=True, stop_supported=False),
            _assessment(),
            PhotoCompletion.SENT_ONLY,
        )
        assert decision is PhotoDecision.UNKNOWN_NO_STOP

    async def test_valid_files_after_cancel_are_kept(self) -> None:
        """取消后到达的合法完成文件保留，不覆盖取消处理。"""
        decision = decide_photo(
            _state(canceled=True, dispatched=True),
            _assessment(complete=True),
            PhotoCompletion.SENT_ONLY,
        )
        assert decision is PhotoDecision.KEEP_FILES_UNDER_CANCEL

    async def test_terminal_is_not_reopened(self) -> None:
        decision = decide_photo(
            _state(action_terminal=True),
            _assessment(complete=True),
            PhotoCompletion.SENT_ONLY,
        )
        assert decision is PhotoDecision.ALREADY_TERMINAL

    async def test_read_error_is_not_empty_directory(self) -> None:
        decision = decide_photo(
            _state(), _assessment(read_error=True), PhotoCompletion.SENT_ONLY
        )
        assert decision is PhotoDecision.VERIFY_RESULTS


class TestRunPhoto:
    async def test_run_photo_has_no_recording_timing(self) -> None:
        """单张任务不产生停止目标或录像时长。"""
        from camctl.capture.photo import PhotoContext, PhotoPorts

        dispatch = PhotoDispatch(
            completion=PhotoCompletion.COMPLETED_ON_RETURN, completed=True
        )
        step = await run_photo(
            PhotoContext(
                state=_state(),
                assessment=_assessment(),
                completion=PhotoCompletion.COMPLETED_ON_RETURN,
                ports=PhotoPorts(driver=_Driver(dispatch), finishes=_Finishes()),
            )
        )
        assert step.stop_target_ns is None
        assert step.phase is not None

    async def test_sent_only_flow_registers_after_assessment(self) -> None:
        from camctl.capture.photo import PhotoContext, PhotoPorts

        dispatch = PhotoDispatch(
            completion=PhotoCompletion.SENT_ONLY, completed=False
        )
        finishes = _Finishes()
        step = await run_photo(
            PhotoContext(
                state=_state(),
                assessment=_assessment(complete=True),
                completion=PhotoCompletion.SENT_ONLY,
                ports=PhotoPorts(driver=_Driver(dispatch), finishes=finishes),
            )
        )
        assert finishes.saved, "只发送契约完成需保存调用结果"


class _Driver:
    def __init__(self, dispatch) -> None:
        self.dispatch = dispatch
        self.calls: list = []

    async def shoot(self, ticket) -> object:
        self.calls.append(ticket)
        return self.dispatch


class _Finishes:
    def __init__(self) -> None:
        self.saved: list = []

    def finish(self, ticket, outcome) -> None:
        self.saved.append((ticket, outcome))
