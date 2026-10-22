"""相机视频结果模式是严格的部署配置，不是拍摄参数。"""

import pytest

from camctl.bootstrap.config import ConfigDefaults, ConfigError, load_config


def _load(video_marker=...):
    device = {"kind": "camera", "driver": "dji-action6"}
    if video_marker is not ...:
        device["video"] = video_marker
    return load_config({"devices": {"cam": device}}, ConfigDefaults())


def test_video_result_mode_defaults_to_single_file_and_accepts_segmented():
    assert _load().devices["cam"]["video"]["result_mode"] == "single_file"
    assert _load({}).devices["cam"]["video"]["result_mode"] == "single_file"
    assert _load({"result_mode": "single_file"}).devices["cam"]["video"]["result_mode"] == "single_file"
    assert _load({"result_mode": "segmented"}).devices["cam"]["video"]["result_mode"] == "segmented"


@pytest.mark.parametrize("video", [
    None, 1, True, [], "segmented", {"result_mode": None},
    {"result_mode": True}, {"result_mode": "unknown"},
    {"result_mode": "single_file", "extra": True},
])
def test_video_result_mode_rejects_invalid_table_type_value_and_fields(video):
    with pytest.raises(ConfigError):
        _load(video)
