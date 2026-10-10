"""固定拍摄定义的生产与读取使用同一契约。"""
from decimal import Decimal, localcontext
from dataclasses import replace

import pytest

from camctl.capture.models import build_capture_spec, validate_capture_spec
from camctl.devices.tasks import CaptureTask, CompletionMode, EndControl, StartReturn


def _task(**changes):
    return replace(CaptureTask(
        action_type="camera_timelapse", target_duration_s=Decimal("1.234"),
        duration_based=True, wait_after_send=True, stop_supported=False,
        end_control=EndControl.DEVICE, start_return_meaning=StartReturn.SENT,
        completion_mode=CompletionMode.TIME_AND_OUTPUTS, result_wait_margin_s=Decimal("0.005"),
    ), **changes)


def test_photo_spec_has_no_device_parameter_copy():
    assert build_capture_spec("camera_take_photo", None) == {}


def _completed_spec():
    return {"duration_based": True, "wait_after_send": False,
        "stop_supported": True, "end_control": 1, "start_return_meaning": 3,
        "completion_mode": 1, "target_duration_ms": 1234}


def test_completed_definition_requires_explicit_full_call_timeout():
    with pytest.raises(ValueError):
        validate_capture_spec("camera_timelapse", _completed_spec())


def test_completed_definition_keeps_exact_full_call_timeout():
    spec = {**_completed_spec(), "start_call_timeout_s": Decimal("600.0001")}
    assert validate_capture_spec("camera_timelapse", spec) == spec


@pytest.mark.parametrize("timeout", [None, True, "600", 0, -1, 600.0,
    Decimal("NaN"), Decimal("Infinity")])
def test_completed_definition_rejects_invalid_full_call_timeout(timeout):
    with pytest.raises(ValueError):
        validate_capture_spec("camera_timelapse", {**_completed_spec(), "start_call_timeout_s": timeout})


def test_driver_full_call_timeout_survives_definition_build_and_read():
    task = _task(start_return_meaning=StartReturn.COMPLETED, wait_after_send=False,
        result_wait_margin_s=None, completion_mode=CompletionMode.DEVICE_EVIDENCE,
        start_call_timeout_s=Decimal("600.0001"))
    spec = build_capture_spec("camera_timelapse", task)
    assert validate_capture_spec("camera_timelapse", spec)["start_call_timeout_s"] == Decimal("600.0001")


@pytest.mark.parametrize("meaning", [StartReturn.SENT, StartReturn.STARTED])
def test_short_control_return_rejects_full_native_call_timeout(meaning):
    spec = {**_completed_spec(), "start_return_meaning": int(meaning),
        "start_call_timeout_s": 600}
    with pytest.raises(ValueError):
        validate_capture_spec("camera_timelapse", spec)


@pytest.mark.parametrize("seconds, milliseconds", [(Decimal(".001"), 1), (Decimal("1.234"), 1234), (Decimal("9223372036854775.807"), 9223372036854775807)])
def test_record_duration_is_exact_and_bounded(seconds, milliseconds):
    with localcontext() as context:
        context.prec = 3
        assert build_capture_spec("camera_record", CaptureTask("camera_record", target_duration_s=seconds, stop_supported=True)) == {"target_duration_ms": milliseconds}


@pytest.mark.parametrize("duration", [None, True, "1", Decimal("1.0005"), 0, -1, Decimal("9223372036854775.808")])
def test_invalid_record_task_cannot_form_spec(duration):
    with pytest.raises(ValueError):
        build_capture_spec("camera_record", CaptureTask("camera_record", target_duration_s=duration, stop_supported=True))


def test_record_requires_explicit_stop_capability():
    with pytest.raises(ValueError):
        build_capture_spec("camera_record", CaptureTask("camera_record", target_duration_s=1))


def test_timelapse_spec_records_applicability_and_integer_enums():
    assert build_capture_spec("camera_timelapse", _task()) == {
        "target_duration_ms":1234, "duration_based":True, "wait_after_send":True,
        "stop_supported":False, "end_control":1, "start_return_meaning":1,
        "completion_mode":2, "result_wait_margin_ms":5,
    }


@pytest.mark.parametrize("end, stop, completion, valid", [(1, False, 1, True), (1, True, 2, True), (2, True, 1, True), (2, False, 1, False), (2, True, 2, False)])
def test_timelapse_control_capability_matrix(end, stop, completion, valid):
    task = _task(end_control=EndControl(end), stop_supported=stop, completion_mode=CompletionMode(completion), wait_after_send=False, result_wait_margin_s=None)
    if valid:
        assert build_capture_spec("camera_timelapse", task)["end_control"] == end
    else:
        with pytest.raises(ValueError):
            build_capture_spec("camera_timelapse", task)


@pytest.mark.parametrize("start", list(StartReturn))
def test_device_task_can_declare_each_start_meaning(start):
    task = _task(start_return_meaning=start, wait_after_send=False, result_wait_margin_s=None,
        completion_mode=CompletionMode.DEVICE_EVIDENCE,
        start_call_timeout_s=Decimal("2") if start is StartReturn.COMPLETED else None)
    assert build_capture_spec("camera_timelapse", task)["start_return_meaning"] == int(start)


def test_non_duration_task_omits_inapplicable_fields():
    task = _task(duration_based=False, wait_after_send=False, target_duration_s=None, result_wait_margin_s=None, completion_mode=CompletionMode.DEVICE_EVIDENCE)
    spec = build_capture_spec("camera_timelapse", task)
    assert "target_duration_ms" not in spec
    assert "result_wait_margin_ms" not in spec


@pytest.mark.parametrize("field, value", [("duration_based", None), ("wait_after_send", 0), ("stop_supported", 1), ("end_control", True), ("start_return_meaning", "sent"), ("completion_mode", None), ("result_wait_margin_s", -1)])
def test_invalid_task_declaration_is_rejected(field, value):
    with pytest.raises(ValueError):
        build_capture_spec("camera_timelapse", _task(**{field:value}))


@pytest.mark.parametrize("change", [{"target_duration_ms":None}, {"target_duration_ms":True}, {"target_duration_ms":0}, {"result_wait_margin_ms":None}, {"duration_based":None}, {"completion_mode":3}, {"unexpected":0}])
def test_invalid_saved_timelapse_spec_is_rejected(change):
    spec = build_capture_spec("camera_timelapse", _task())
    with pytest.raises(ValueError):
        validate_capture_spec("camera_timelapse", {**spec, **change})


@pytest.mark.parametrize("field", ["duration_based", "wait_after_send", "target_duration_ms", "result_wait_margin_ms", "end_control", "stop_supported", "start_return_meaning", "completion_mode"])
def test_required_saved_timelapse_field_cannot_be_defaulted(field):
    spec = build_capture_spec("camera_timelapse", _task())
    del spec[field]
    with pytest.raises(ValueError):
        validate_capture_spec("camera_timelapse", spec)


@pytest.mark.parametrize("value", [None, [], "{}", {"effective_params":{}}, {"target_duration_ms":1}])
def test_photo_definition_accepts_only_empty_object(value):
    with pytest.raises(ValueError):
        validate_capture_spec("camera_take_photo", value)


@pytest.mark.parametrize("margin", [0, Decimal("0.001"), Decimal("9223372036854775.807")])
def test_wait_margin_can_be_zero_or_the_exact_upper_bound(margin):
    expected = {0:0, Decimal("0.001"):1, Decimal("9223372036854775.807"):9223372036854775807}[margin]
    assert build_capture_spec("camera_timelapse", _task(result_wait_margin_s=margin))["result_wait_margin_ms"] == expected


@pytest.mark.parametrize("change", [
    {"wait_after_send":True,"duration_based":False},
    {"end_control":2,"stop_supported":True,"start_return_meaning":3},
    {"end_control":2,"stop_supported":True,"start_return_meaning":1},
    {"start_return_meaning":2},
    {"result_wait_margin_ms":9223372036854775808},
    {"wait_after_send":False},
])
def test_saved_wait_conditions_cannot_contradict_each_other(change):
    spec = {"duration_based":True,"wait_after_send":True,"stop_supported":False,
        "end_control":1,"start_return_meaning":1,"completion_mode":2,
        "target_duration_ms":1234,"result_wait_margin_ms":0}
    with pytest.raises(ValueError):
        validate_capture_spec("camera_timelapse", {**spec, **change})
