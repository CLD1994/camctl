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
    activity_capabilities,
    build_capture_spec,
    validate_capture_spec,
)
from camctl.capture.handlers import capture_handler, route_completion
from camctl.devices.tasks import CaptureStorage, CaptureTask, StartReturn


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

    def test_legacy_photo_without_task_keeps_an_empty_execution_definition(self) -> None:
        assert build_capture_spec("camera_take_photo", None) == {}

    def test_host_photo_task_saves_its_storage_and_exact_start_deadline(self) -> None:
        task = CaptureTask(
            "camera_take_photo",
            output_storage=CaptureStorage.HOST,
            start_call_timeout_s=Decimal("180"),
        )
        assert build_capture_spec("camera_take_photo", task) == {
            "output_storage": "host",
            "start_call_timeout_s": Decimal("180"),
        }

    def test_empty_photo_task_factory_keeps_legacy_empty_execution_definition(self) -> None:
        assert build_capture_spec("camera_take_photo", CaptureTask("camera_take_photo")) == {}

    @pytest.mark.parametrize(
        "task",
        [
            CaptureTask("camera_take_photo", output_storage=CaptureStorage.HOST),
            CaptureTask("camera_take_photo", start_call_timeout_s=Decimal("180")),
        ],
        ids=["storage-without-deadline", "deadline-without-storage"],
    )
    def test_photo_factory_rejects_partial_host_task_definition(self, task) -> None:
        with pytest.raises(ValueError):
            build_capture_spec("camera_take_photo", task)

    def test_photo_factory_rejects_task_for_another_action(self) -> None:
        with pytest.raises(ValueError):
            build_capture_spec("camera_take_photo", CaptureTask("camera_record"))

    def test_photo_factory_rejects_recording_fields_in_its_single_task(self) -> None:
        from camctl.devices.tasks import CaptureStorage

        with pytest.raises(ValueError):
            build_capture_spec(
                "camera_take_photo",
                CaptureTask(
                    "camera_take_photo",
                    output_storage=CaptureStorage.HOST,
                    start_call_timeout_s=Decimal("180"),
                    target_duration_s=Decimal("4"),
                ),
            )

    @pytest.mark.parametrize(
        "spec",
        [
            {"output_storage": "host"},
            {"start_call_timeout_s": Decimal("180")},
            {"output_storage": "device", "start_call_timeout_s": Decimal("180")},
            {"output_storage": "host", "start_call_timeout_s": Decimal("180"), "extra": 1},
        ],
    )
    def test_photo_execution_definition_rejects_missing_or_inapplicable_fields(self, spec):
        with pytest.raises(ValueError):
            validate_capture_spec("camera_take_photo", spec)

    @pytest.mark.parametrize("timeout", [True, 0, -1, 1.25, Decimal("NaN"), Decimal("Infinity")])
    def test_photo_start_deadline_must_be_finite_positive_and_exact(self, timeout):
        with pytest.raises(ValueError):
            validate_capture_spec(
                "camera_take_photo", {"output_storage": "host", "start_call_timeout_s": timeout}
            )

    def test_photo_activity_contract_keeps_completed_without_stop_or_state_query(self):
        capabilities = activity_capabilities(
            "camera_take_photo",
            {"output_storage": "host", "start_call_timeout_s": Decimal("180")},
        )
        assert capabilities.state_query_supported == 0
        assert capabilities.stop_supported == 0
        assert capabilities.start_return_meaning == int(StartReturn.COMPLETED)


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
