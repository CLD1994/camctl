"""按设备部署选择经真实目录和受理冻结，不依赖设备调用。"""

from copy import deepcopy
from pathlib import Path

import pytest

from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.json_values import parse_exact_json
from camctl.devices import definitions_runtime
from camctl.devices.catalog import build_catalog
from camctl.devices.drivers import runtime as drivers_runtime
from camctl.devices.drivers.adb_cameras.registration import register_builtin_camera_drivers
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _params
from ..scheduling.test_start_action import _accept_plan, owned


def catalog(monkeypatch, driver, modes):
    monkeypatch.setattr(definitions_runtime, "_definitions", {})
    monkeypatch.setattr(drivers_runtime, "_entries", {})
    config = load_config({"devices": {
        f"cam-{index}": {"kind": "camera", "driver": driver,
                         "adb": {"serial": f"serial-{index}"},
                         "video": {"result_mode": mode}}
        for index, mode in enumerate(modes, 1)
    }}, ConfigDefaults())
    register_builtin_camera_drivers(config)
    return build_catalog(config, definitions_runtime.current_driver_definitions())


def definitions(owned):
    return tuple((identity, parse_exact_json(spec)) for identity, spec in
                 owned.connection.execute("SELECT id,execution_spec_json FROM actions ORDER BY id"))


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_camera_modes_are_independent_and_accepted_definitions_stay_frozen(
        owned, tmp_path, monkeypatch, driver, action_type):
    initial = catalog(monkeypatch, driver, ("single_file", "segmented"))
    body = _plan_body()
    action = body["actions"][0]
    action.update(type=action_type, params=_params(driver, action_type))
    body["actions"] = [dict(deepcopy(action), name=f"相机{index}", device_id=f"cam-{index}")
                       for index in (1, 2)]
    await _accept_plan(owned, tmp_path, body, initial)
    frozen = definitions(owned)
    assert [spec["video_result_mode"] for _, spec in frozen] == ["single_file", "segmented"]
    assert frozen[0][1].get("result_policy") is None
    assert frozen[1][1]["result_policy"]
    changed = catalog(monkeypatch, driver, ("segmented", "single_file"))
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    restarted = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        await _accept_plan(restarted, tmp_path, body, changed)
        assert definitions(restarted) == frozen
        new = deepcopy(body)
        new["request_id"] = "2"
        await _accept_plan(restarted, tmp_path, new, changed)
        current = definitions(restarted)
    finally:
        restarted.connection.close()
    assert current[:2] == frozen
    assert [spec["video_result_mode"] for _, spec in current[2:]] == ["segmented", "single_file"]
