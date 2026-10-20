"""正式内置录像契约的静态输入与能力边界。"""

from decimal import Decimal

import pytest

from camctl.acceptance.schema import validate_precise
from camctl.capture.models import build_capture_spec
from camctl.contracts.json_values import parse_exact_json
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.registration import builtin_camera_contracts


def _contract(model):
    return next(contract for contract in builtin_camera_contracts() if contract.driver_id == model)


def _params():
    return {"type": "action6_record", "duration_s": 10, "resolution": "4k30",
            "fov": "wide", "stabilization": "off", "aperture": "f2.8", "bitrate": "high",
            "exposure": {"mode": "auto", "compensation_ev": 0}}


def test_action6_builtin_exposes_ordinary_recording_and_formal_timelapse():
    capabilities = _contract(CameraModel.ACTION6).capabilities()
    assert {capability.action_type for capability in capabilities if callable(capability.task_factory)} == {
        "camera_record", "camera_timelapse"}


def test_action6_formal_task_explains_requested_settings_readback_estimate_and_cancellation():
    capability = next(cap for cap in _contract(CameraModel.ACTION6).capabilities()
                      if cap.parameter_type == "action6_record")
    task = capability.task_factory({"type": "action6_record", "duration_s": 10})

    assert task.stop_supported is True
    assert task.cancel_completion_policy is None
    for fact in ("1/60 秒", "5200 K", "请求", "独立读回", "固定假定参考码率 130 Mbps",
                 "不是实测平均值", "启动后", "取消"):
        assert fact in capability.description


def test_action6_task_freezes_internal_scope_and_five_second_completion_rule():
    factory = _contract(CameraModel.ACTION6).task_factories.get("action6_record")
    assert callable(factory)
    task = factory(_params())
    assert task.target_duration_s == 10
    assert task.stop_supported is True
    assert task.file_completion_wait_s == Decimal("5")
    assert task.ownership_mode == 2
    assert task.output_scope == {"directories": ["/mnt/media_rw/emulated/DCIM"]}
    assert task.product_rules == ({"kind": "video", "format_id": "mp4", "min_count": 1,
                                  "exact_count": 1, "require_pairing": False},)


@pytest.mark.parametrize("outputs,exposure,photo_format", [
    ("video", {"mode": "manual", "iso": 800}, None),
    ("video", {"mode": "auto"}, None),
    ("video_raw", {"mode": "manual", "iso": 800}, "dng"),
    ("video_raw", {"mode": "auto"}, "dng"),
    ("video_jpeg", {"mode": "manual", "iso": 800}, "jpeg"),
    ("video_jpeg", {"mode": "auto"}, "jpeg"),
])
def test_action6_timelapse_freezes_finite_task_and_selected_nonempty_formats(
        outputs, exposure, photo_format):
    contract = _contract(CameraModel.ACTION6)
    capability = next(cap for cap in contract.capabilities()
                      if cap.action_type == "camera_timelapse")
    params = {"type": "action6_timelapse", "interval_s": 8, "duration_s": 1800,
              "outputs": outputs, "exposure": exposure}
    validate_precise(capability.schema, params)

    assert callable(capability.task_factory)
    task = capability.task_factory(params)

    assert task.action_type == "camera_timelapse"
    assert task.target_duration_s == 1800 and task.stop_supported is True
    assert task.duration_based is True and task.wait_after_send is True
    assert task.end_control.value == 1 and task.start_return_meaning.value == 1
    assert task.completion_mode.value == 2
    assert task.startup_margin_ms == 0
    assert task.result_wait_margin_s == task.file_completion_wait_s == Decimal("30")
    assert task.cancel_completion_policy == "finite_device_end"
    assert task.output_scope == {"directories": ["/mnt/media_rw/emulated/DCIM"]}
    expected = ({"kind": "video", "format_id": "mp4", "min_count": 1,
                 "exact_count": None, "require_pairing": False, "min_size_bytes": 1},)
    if photo_format is not None:
        expected += ({"kind": "photo", "format_id": photo_format, "min_count": 1,
                      "exact_count": None, "require_pairing": False, "min_size_bytes": 1},)
    assert task.product_rules == expected


def test_action6_timelapse_factory_rejects_missing_outputs_without_a_default():
    factory = _contract(CameraModel.ACTION6).task_factories.get("action6_timelapse")
    assert callable(factory)

    with pytest.raises(ValueError, match="outputs"):
        factory({"type": "action6_timelapse", "interval_s": 8, "duration_s": 1800,
                 "exposure": {"mode": "auto"}})


def test_action6_timelapse_description_uses_static_metadata_and_exports_without_fake_outputs():
    capabilities = _contract(CameraModel.ACTION6).capabilities()
    capability = next(cap for cap in capabilities if cap.action_type == "camera_timelapse")

    assert callable(capability.task_factory)
    assert "有限" in capability.description and "30" in capability.description
    assert "video_raw" in capability.description and "DNG" in capability.description


@pytest.mark.parametrize(("model", "action_type"), [
    (CameraModel.ACTION6, "camera_record"),
    (CameraModel.ACTION6, "camera_timelapse"),
    (CameraModel.OSMO360II, "camera_record"),
    (CameraModel.OSMO360II, "camera_timelapse"),
])
def test_formal_action_descriptions_do_not_keep_candidate_readiness_caveats(model, action_type):
    capability = next(cap for cap in _contract(model).capabilities()
                      if cap.action_type == action_type)

    assert callable(capability.task_factory)
    for candidate_caveat in (
        "调用响应待设备核实。",
        "参数合法不表示已有完整命令映射；",
        "正常结束及文件写完依据待设备核实。",
    ):
        assert candidate_caveat not in capability.description


@pytest.mark.parametrize("model", [CameraModel.ACTION6, CameraModel.OSMO360II])
def test_candidate_timelapse_descriptions_keep_readiness_caveats_without_a_factory(model):
    from camctl.devices.drivers.adb_cameras.contracts import CameraContract

    capability = next(cap for cap in CameraContract(model).capabilities()
                      if cap.action_type == "camera_timelapse")

    assert capability.task_factory is None
    assert "参数合法不表示已有完整命令映射；" in capability.description
    assert "正常结束及文件写完依据待设备核实。" in capability.description


@pytest.mark.parametrize("suffix,format_id", [
    (".MP4", "mp4"), (".DNG", "dng"), (".JPG", "jpeg"),
])
def test_action6_format_mapping_resolves_the_documented_suffix_without_case_sensitivity(
        suffix, format_id):
    from camctl.devices.drivers.adb_cameras.action6_formats import format_for_suffix

    file_format = format_for_suffix(suffix)

    assert file_format is not None and file_format.format_id == format_id
    assert format_for_suffix(suffix + "x") is None


@pytest.mark.parametrize("duration_json, target_duration_ms", [
    ("1", 1000), ("1.0", 1000), ("1e0", 1000),
    ("9223372036854775", 9223372036854775000),
    ("9223372036854775.0", 9223372036854775000),
])
def test_action6_integer_seconds_reach_execution_definition_exactly(duration_json, target_duration_ms):
    capability = next(cap for cap in _contract(CameraModel.ACTION6).capabilities()
                      if cap.action_type == "camera_record")
    params = {**_params(), "duration_s": parse_exact_json(duration_json)}
    validate_precise(capability.schema, params)

    spec = build_capture_spec("camera_record", capability.task_factory(params))

    assert spec["target_duration_ms"] == target_duration_ms


def test_action6_declaration_has_complete_files_without_activity_query():
    declaration = _contract(CameraModel.ACTION6).declaration
    assert declaration.control_supported and declaration.stop_supported and declaration.result_supported
    assert declaration.directory_supported and declaration.read_supported and declaration.digest_supported
    assert not declaration.query_supported
    assert declaration.file_query_supported and declaration.delete_supported
    assert not declaration.capture_read_parallel_supported


def test_action6_result_reader_is_declared_as_a_composite_operation():
    assert callable(getattr(_contract(CameraModel.ACTION6), "result_reader", None))


def test_action6_digest_deadline_is_finite_and_positive_for_source_sizes():
    timeout = _contract(CameraModel.ACTION6).digest_timeout_s
    assert callable(timeout)
    for size in (0, 1, 65536, 64637726):
        value = timeout(size)
        assert isinstance(value, Decimal) and value.is_finite() and value > 0


def test_osmo_builtin_exports_record_and_timelapse_from_one_complete_contract():
    capabilities = _contract(CameraModel.OSMO360II).capabilities()
    assert {capability.action_type for capability in capabilities} == {"camera_record", "camera_timelapse"}
    assert all(callable(capability.task_factory) and capability.preview_supported for capability in capabilities)


def test_new_action6_record_freezes_command_acceptance_and_startup_margin():
    factory = _contract(CameraModel.ACTION6).task_factories['action6_record']
    spec = build_capture_spec('camera_record', factory(_params()))
    assert spec['start_return_meaning'] == 1
    assert spec['startup_margin_ms'] == 1000
    assert spec['target_duration_ms'] == 10000


def test_action6_file_capability_comes_from_factories_evidence_and_recovery():
    from camctl.devices.drivers.adb_cameras.contracts import CameraCall
    contract = _contract(CameraModel.ACTION6)
    for kind in (CameraCall.FILE_QUERY, CameraCall.DELETE):
        assert callable(contract.commands.get(kind))
    assert CameraCall.QUERY not in contract.commands
    assert {"query", "delete", "control", "stop", "result", "read"} <= contract.recovery_operations
    for operation in ("query", "delete"):
        assert contract.evidence.contract(operation + "_returned", 1).operation == operation
        assert contract.evidence.contract(operation + "_foreground_recovery", 1).operation == operation
    assert contract.evidence.contract("adb_foreground_recovery", 1).operation == "delete"
    for cap in contract.capabilities():
        assert cap.preview_supported is True
        assert "LRF" in cap.description and "原片" in cap.description
        assert "运行假设" in cap.description
