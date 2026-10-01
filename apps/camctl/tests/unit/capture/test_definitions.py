"""C1 拍摄专属定义及能力路由的单元测试。

三种拍摄动作按各自完成声明路由对应流程；未支持类型不默认录像；
target_duration_ms 精确（1.5 秒=1500ms、1.0005 秒拒绝）；受理
失败定义不构造。
"""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from camctl.capture.models import (
    CaptureCompletion,
    CaptureDefinition,
    CaptureInput,
)
from camctl.capture.handlers import capture_handler, route_completion
from camctl.devices.tasks import CaptureTask


def _input(action_type: str = "camera_record", **params) -> CaptureInput:
    return CaptureInput(action_type=action_type, effective_params=params,
                        task=CaptureTask(action_type, **params) if action_type != "camera_take_photo" else None)


class TestCaptureDefinition:
    def test_record_definition_keeps_milliseconds_at_low_precision(self) -> None:
        with localcontext() as context:
            context.prec = 3
            definition = CaptureDefinition.build(
                _input(target_duration_s=Decimal("1.234"), stop_supported=True)
            )
        assert definition.target_duration_ms == 1234

    def test_record_definition_rejects_tiny_fractional_millisecond(self) -> None:
        with pytest.raises(ValueError):
            CaptureDefinition.build(
                _input(
                    target_duration_s=Decimal("1.0000000000000000000000000001"),
                    stop_supported=True,
                )
            )

    def test_record_definition_with_exact_duration(self) -> None:
        definition = CaptureDefinition.build(
            _input(target_duration_s=Decimal("1.5"), stop_supported=True)
        )
        assert definition.target_duration_ms == 1500

    def test_sub_millisecond_duration_rejected(self) -> None:
        with pytest.raises(ValueError):
            CaptureDefinition.build(
                _input(target_duration_s=Decimal("1.0005"), stop_supported=True)
            )

    def test_record_requires_stop_capability(self) -> None:
        with pytest.raises(ValueError, match="stop"):
            CaptureDefinition.build(
                _input(target_duration_s=Decimal("2"), stop_supported=False)
            )

    def test_photo_has_no_duration(self) -> None:
        definition = CaptureDefinition.build(_input("camera_take_photo"))
        assert definition.target_duration_ms is None


class TestCompletionRouting:
    def test_capability_routes_own_completion(self) -> None:
        cases = {
            "camera_take_photo": CaptureCompletion.DEVICE_EVIDENCE,
            "camera_record": CaptureCompletion.DEVICE_EVIDENCE,
            "camera_timelapse": CaptureCompletion.TIME_AND_OUTPUTS,
        }
        for action_type, declared in cases.items():
            assert route_completion(action_type, declared) is declared

    def test_declared_mode_preserved(self) -> None:
        assert (
            route_completion("camera_record", CaptureCompletion.TIME_AND_OUTPUTS)
            is CaptureCompletion.TIME_AND_OUTPUTS
        )

    def test_unknown_type_rejected_not_defaulting_to_record(self) -> None:
        with pytest.raises(ValueError):
            route_completion("obtain_action_outputs", CaptureCompletion.DEVICE_EVIDENCE)


class TestHandlerRouting:
    def test_handler_per_action_type(self) -> None:
        for action_type in (
            "camera_take_photo", "camera_record", "camera_timelapse"
        ):
            assert callable(capture_handler(action_type))

    def test_unknown_handler_rejected(self) -> None:
        with pytest.raises(KeyError):
            capture_handler("cancel_task")
