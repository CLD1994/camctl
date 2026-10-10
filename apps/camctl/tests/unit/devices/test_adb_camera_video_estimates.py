"""真实相机的参考估算声明与拍摄资格边界。"""

from dataclasses import replace

import pytest

from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.definitions import candidate_capabilities
from camctl.devices.drivers.adb_cameras.registration import builtin_camera_contracts


def _candidate(model, action_type):
    return next(capability for capability in candidate_capabilities(model)
                if capability.action_type == action_type)


@pytest.mark.parametrize("model, bitrate", [
    (CameraModel.ACTION6, 130),
    (CameraModel.OSMO360II, 170),
])
def test_recording_candidate_declares_fixed_assumed_video_reference(model, bitrate):
    capability = _candidate(model, "camera_record")

    assert capability.video_size_estimate == {
        "bitrate_mbps": bitrate,
        "duration": {
            "method": "direct",
            "seconds": {"source": "parameter", "path": "/duration_s"},
        },
    }
    assert capability.task_factory is None


@pytest.mark.parametrize("model, bitrate", [
    (CameraModel.ACTION6, 80),
    (CameraModel.OSMO360II, 300),
])
def test_timelapse_candidate_uses_capture_interval_and_fixed_playback_rate(model, bitrate):
    capability = _candidate(model, "camera_timelapse")

    assert capability.video_size_estimate == {
        "bitrate_mbps": bitrate,
        "duration": {
            "method": "timelapse_interval",
            "capture_seconds": {"source": "parameter", "path": "/duration_s"},
            "interval_seconds": {"source": "parameter", "path": "/interval_s"},
            "playback_fps": {"source": "constant", "value": 30},
        },
    }
    assert capability.task_factory is None


@pytest.mark.parametrize("model", list(CameraModel))
@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
def test_reference_description_matches_same_definition_and_explains_assumption(model, action_type):
    capability = _candidate(model, action_type)
    estimate = capability.video_size_estimate

    assert f"{estimate['bitrate_mbps']} Mbps" in capability.description
    assert "假定" in capability.description
    assert "实测" in capability.description
    assert "目标视频" in capability.description
    assert "预览视频" in capability.description
    assert "独立照片" in capability.description
    if action_type == "camera_timelapse":
        assert str(estimate["duration"]["playback_fps"]["value"]) in capability.description
        assert "一帧" in capability.description


@pytest.mark.parametrize("model", list(CameraModel))
@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
def test_video_estimate_metadata_does_not_open_capture_parameters(model, action_type):
    capability = _candidate(model, action_type)

    assert capability.video_size_estimate is not None
    assert not {"video_size_estimate", "bitrate_mbps", "playback_fps"}.intersection(
        capability.schema["properties"])
    assert capability.defaults == {}


@pytest.mark.parametrize("model", list(CameraModel))
@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
def test_video_estimates_are_independent_between_generated_definitions(model, action_type):
    first = _candidate(model, action_type)
    first.video_size_estimate["bitrate_mbps"] = 1
    first.video_size_estimate["duration"].clear()

    second = _candidate(model, action_type)

    assert second.video_size_estimate["bitrate_mbps"] != 1
    assert second.video_size_estimate["duration"]["method"] == (
        "direct" if action_type == "camera_record" else "timelapse_interval")


def test_adding_video_estimates_preserves_builtin_capture_readiness_and_metadata():
    capabilities = [(contract.driver_id, capability)
                    for contract in builtin_camera_contracts()
                    for capability in contract.capabilities()]

    assert [(model, capability.parameter_type)
            for model, capability in capabilities if callable(capability.task_factory)] == [
        (CameraModel.ACTION6, "action6_record")]
    for model, capability in capabilities:
        candidate = _candidate(model, capability.action_type)
        assert capability.video_size_estimate == candidate.video_size_estimate
        assert f"{candidate.video_size_estimate['bitrate_mbps']} Mbps" in capability.description


def test_action6_execution_task_excludes_video_estimate_metadata():
    contract = next(contract for contract in builtin_camera_contracts()
                    if contract.driver_id == CameraModel.ACTION6)
    capability = next(capability for capability in contract.capabilities()
                      if capability.action_type == "camera_record")
    assert capability.video_size_estimate is not None

    task = capability.task_factory({"type": "action6_record", "duration_s": 10,
        "resolution": "4k30", "fov": "wide", "stabilization": "off",
        "exposure": {"mode": "auto", "compensation_ev": 0}})

    assert task.target_duration_s == 10
    assert "video_size_estimate" not in vars(task)


def test_ready_timelapse_contract_preserves_reference_description_without_claiming_completion():
    contract = next(contract for contract in builtin_camera_contracts()
                    if contract.driver_id == CameraModel.ACTION6)
    contract = replace(contract, task_factories={**contract.task_factories,
        "action6_timelapse": lambda params: None})

    capability = next(capability for capability in contract.capabilities()
                      if capability.action_type == "camera_timelapse")

    assert callable(capability.task_factory)
    assert f"{capability.video_size_estimate['bitrate_mbps']} Mbps" in capability.description
    assert "一帧" in capability.description
    assert "假定" in capability.description
    assert "正常结束及文件写完依据待设备核实。" not in capability.description
