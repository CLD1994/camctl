"""固定拍摄定义的生产与读取使用同一契约。"""
from decimal import Decimal, localcontext
from dataclasses import replace

import pytest

from camctl.capture.models import CaptureDefinition, CaptureInput, build_capture_spec, validate_capture_spec
from camctl.devices.tasks import (CaptureTask, CompletionMode, EndControl,
                                  StartReturn, VideoResultMode)


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
    return {"video_result_mode": "single_file", "duration_based": True, "wait_after_send": False,
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
        assert build_capture_spec("camera_record", CaptureTask("camera_record", target_duration_s=seconds, stop_supported=True)) == {"target_duration_ms": milliseconds, "video_result_mode": "single_file"}


@pytest.mark.parametrize("duration", [None, True, "1", Decimal("1.0005"), 0, -1, Decimal("9223372036854775.808")])
def test_invalid_record_task_cannot_form_spec(duration):
    with pytest.raises(ValueError):
        build_capture_spec("camera_record", CaptureTask("camera_record", target_duration_s=duration, stop_supported=True))


def test_record_requires_explicit_stop_capability():
    with pytest.raises(ValueError):
        build_capture_spec("camera_record", CaptureTask("camera_record", target_duration_s=1))


@pytest.mark.parametrize("seconds,milliseconds", [
    (Decimal(".001"), 1), (Decimal("1.234"), 1234), (5, 5000),
    (Decimal("9223372036854775.807"), 9223372036854775807),
])
def test_record_file_completion_wait_is_fixed_without_decimal_rounding(seconds, milliseconds):
    task = CaptureTask("camera_record", target_duration_s=1, stop_supported=True,
        file_completion_wait_s=seconds)
    with localcontext() as context:
        context.prec = 3
        spec = build_capture_spec("camera_record", task)
    assert spec == {"target_duration_ms": 1000, "video_result_mode": "single_file",
                    "file_completion_wait_ms": milliseconds}
    assert validate_capture_spec("camera_record", spec) == spec


@pytest.mark.parametrize("wait", [True, "5", 0, -1, Decimal(".0005"),
    Decimal("NaN"), Decimal("Infinity"), Decimal("9223372036854775.808")])
def test_record_rejects_file_completion_wait_that_cannot_be_positive_exact_milliseconds(wait):
    task = CaptureTask("camera_record", target_duration_s=1, stop_supported=True,
        file_completion_wait_s=wait)
    with pytest.raises(ValueError):
        build_capture_spec("camera_record", task)


@pytest.mark.parametrize("wait", [None, True, "5000", 0, -1, Decimal("1.5"),
    Decimal("NaN"), Decimal("Infinity"), 9223372036854775808])
def test_saved_record_rejects_invalid_explicit_file_completion_wait(wait):
    with pytest.raises(ValueError):
        validate_capture_spec("camera_record", {
            "target_duration_ms": 1000, "file_completion_wait_ms": wait})


def test_record_without_file_completion_wait_preserves_stop_completion_contract():
    assert validate_capture_spec("camera_record", {
        "target_duration_ms": 1000, "video_result_mode": "single_file"}) == {
        "target_duration_ms": 1000, "video_result_mode": "single_file"}
    assert build_capture_spec("camera_record", CaptureTask("camera_record",
        target_duration_s=1, stop_supported=True, file_completion_wait_s=None)) == {
            "target_duration_ms": 1000, "video_result_mode": "single_file"}


def test_timelapse_spec_records_applicability_and_integer_enums():
    assert build_capture_spec("camera_timelapse", _task()) == {
        "target_duration_ms":1234, "video_result_mode":"single_file",
        "duration_based":True, "wait_after_send":True,
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
    spec = {"video_result_mode":"single_file","duration_based":True,
        "wait_after_send":True,"stop_supported":False,
        "end_control":1,"start_return_meaning":1,"completion_mode":2,
        "target_duration_ms":1234,"result_wait_margin_ms":0}
    with pytest.raises(ValueError):
        validate_capture_spec("camera_timelapse", {**spec, **change})


def test_native_timelapse_freezes_independent_stop_wait_and_delayed_cancel_policy():
    from camctl.devices.tasks import CancelCompletionPolicy
    spec = build_capture_spec('camera_timelapse', _task(stop_supported=True,
        file_completion_wait_s=30, cancel_completion_policy=CancelCompletionPolicy.FINITE_DEVICE_END))
    assert spec['file_completion_wait_ms'] == 30000
    assert spec['cancel_completion_policy'] == 'finite_device_end'
    assert validate_capture_spec('camera_timelapse', spec) == spec


@pytest.mark.parametrize('changes', [
    {'end_control': 2, 'completion_mode': 1, 'wait_after_send': False},
    {'start_return_meaning': 2, 'wait_after_send': False},
    {'completion_mode': 1}, {'stop_supported': False},
    {'cancel_completion_policy': None}, {'cancel_completion_policy': 'other'},
])
def test_delayed_cancel_policy_rejects_incompatible_native_contract(changes):
    spec = {'duration_based': True, 'wait_after_send': True, 'stop_supported': True,
        'end_control': 1, 'start_return_meaning': 1, 'completion_mode': 2,
        'target_duration_ms': 300000, 'result_wait_margin_ms': 30000,
        'file_completion_wait_ms': 30000, 'cancel_completion_policy': 'finite_device_end'}
    with pytest.raises(ValueError):
        validate_capture_spec('camera_timelapse', {**spec, **changes})


@pytest.mark.parametrize('wait', [None, True, '30000', 0, -1, Decimal('1.5')])
def test_saved_native_stop_wait_must_be_positive_exact_milliseconds(wait):
    spec = build_capture_spec('camera_timelapse', _task(stop_supported=True))
    with pytest.raises(ValueError):
        validate_capture_spec('camera_timelapse', {**spec, 'file_completion_wait_ms': wait})


def test_no_stop_native_task_cannot_freeze_stop_file_wait():
    with pytest.raises(ValueError):
        build_capture_spec('camera_timelapse', _task(file_completion_wait_s=30))


def test_photo_cannot_ignore_native_cancel_policy():
    from camctl.devices.tasks import CancelCompletionPolicy
    with pytest.raises(ValueError):
        build_capture_spec('camera_take_photo', CaptureTask('camera_take_photo',
            cancel_completion_policy=CancelCompletionPolicy.FINITE_DEVICE_END))


@pytest.mark.parametrize('margin_ms', [0, 1000])
def test_new_record_freezes_sent_and_startup_margin_together(margin_ms):
    from camctl.capture.models import activity_capabilities
    spec = {"target_duration_ms": 10000, "video_result_mode": "single_file",
            "start_return_meaning": 1, "startup_margin_ms": margin_ms}
    assert validate_capture_spec("camera_record", spec) == spec
    assert activity_capabilities("camera_record", spec).start_return_meaning == 1
    assert activity_capabilities("camera_record", {
        "target_duration_ms": 10000, "video_result_mode": "single_file"
    }).start_return_meaning == 2


@pytest.mark.parametrize("changes", [
    {"start_return_meaning": 1}, {"startup_margin_ms": 1000},
    {"start_return_meaning": 2, "startup_margin_ms": 1000},
    *({"start_return_meaning": 1, "startup_margin_ms": value} for value in
      [True, -1, Decimal(".5"), None]),
])
def test_new_record_rejects_partial_or_invalid_startup_contract(changes):
    with pytest.raises(ValueError):
        validate_capture_spec("camera_record", {
            "target_duration_ms": 10000, "video_result_mode": "single_file", **changes})


@pytest.mark.parametrize('margin_ms', [0, 1000])
def test_native_startup_margin_preserves_explicit_nonnegative_milliseconds(margin_ms):
    old = build_capture_spec("camera_timelapse", _task())
    assert "startup_margin_ms" not in validate_capture_spec("camera_timelapse", old)
    assert validate_capture_spec("camera_timelapse", {**old, "startup_margin_ms": margin_ms})["startup_margin_ms"] == margin_ms
    assert build_capture_spec("camera_timelapse", _task(startup_margin_ms=margin_ms))["startup_margin_ms"] == margin_ms


@pytest.mark.parametrize('margin_ms', [True, -1, Decimal('.5'), None, 9223372036854775808])
def test_native_startup_margin_rejects_invalid_explicit_milliseconds(margin_ms):
    old = build_capture_spec("camera_timelapse", _task())
    with pytest.raises(ValueError):
        validate_capture_spec("camera_timelapse", {**old, "startup_margin_ms": margin_ms})


@pytest.mark.parametrize('margin_ms', [0, 1000])
def test_task_factory_can_freeze_startup_margin(margin_ms):
    task = CaptureTask("camera_record", target_duration_s=10, stop_supported=True,
        start_return_meaning=StartReturn.SENT, startup_margin_ms=margin_ms)
    assert build_capture_spec("camera_record", task) == {
        "target_duration_ms": 10000, "video_result_mode": "single_file",
        "start_return_meaning": 1, "startup_margin_ms": margin_ms}


@pytest.mark.parametrize(("parameter_type", "action_type", "policy"), [
    ("osmo360ii_record", "camera_record", "osmo360ii_record_segments_v1"),
    ("osmo360ii_timelapse", "camera_timelapse", "osmo_empty_tail_classification_v1"),
])
def test_osmo_result_policy_survives_task_build_acceptance_and_saved_definition_read(
    parameter_type, action_type, policy
):
    from camctl.devices.drivers.adb_cameras.osmo360ii_capture import osmo360ii_contract

    task = osmo360ii_contract().task_factories[parameter_type]({"duration_s": 12},
        result_mode=VideoResultMode.SEGMENTED)
    spec = build_capture_spec(action_type, task)
    assert task.result_policy.value == policy
    assert spec["result_policy"] == policy
    assert validate_capture_spec(action_type, spec)["result_policy"] == policy
    definition = CaptureDefinition.build(CaptureInput(action_type, {}, task))
    assert definition.result_policy.value == policy


def test_capture_result_policy_rejects_unknown_policy_and_fixed_segment_count():
    from camctl.devices.drivers.adb_cameras.osmo360ii_capture import osmo360ii_contract

    task = osmo360ii_contract().task_factories["osmo360ii_record"]({"duration_s": 12},
        result_mode=VideoResultMode.SEGMENTED)
    spec = build_capture_spec("camera_record", task)
    invalid_policy = {**spec, "result_policy": "unregistered_policy"}
    with pytest.raises(ValueError, match="策略未登记"):
        validate_capture_spec("camera_record", invalid_policy)

    limited_products = {**spec, "product_rules": [
        {**spec["product_rules"][0], "exact_count": 1}
    ]}
    with pytest.raises(ValueError, match="不得限制段数"):
        validate_capture_spec("camera_record", limited_products)


def test_capture_result_policy_cannot_be_reused_for_another_capture_action():
    from camctl.devices.drivers.adb_cameras.osmo360ii_capture import osmo360ii_contract

    task = osmo360ii_contract().task_factories["osmo360ii_timelapse"]({"duration_s": 12},
        result_mode=VideoResultMode.SEGMENTED)
    spec = build_capture_spec("camera_timelapse", task)
    with pytest.raises(ValueError, match="只适用于 camera_record"):
        validate_capture_spec("camera_timelapse", {
            **spec, "result_policy": "osmo360ii_record_segments_v1"
        })
