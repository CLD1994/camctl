"""真实目录、受理和历史保存组合；任务完成契约仅由测试替身声明。"""
from dataclasses import replace
from copy import deepcopy

import pytest

from camctl.acceptance.definitions import read_action_spec
from camctl.acceptance.service import PlanDisposition
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.devices.catalog import DriverDefinition, DriverDefinitions, build_catalog
from camctl.devices.drivers.adb_cameras import definitions
from camctl.devices.tasks import CaptureTask, CompletionMode, EndControl, StartReturn
from camctl.persistence.repositories.history import HistoryRepository
from .test_acceptance import _accept, _plan_body, environment
from .test_fields import _action_facts

pytestmark = pytest.mark.asyncio


def _catalog(driver, *, duration_default=None, device_id="cam-1", baseline=True):
    """候选 Schema 复用生产定义；替身显式提供软件测试所需的结束契约。"""
    def task(params):
        action_type = "camera_record" if params["type"].endswith("_record") else "camera_timelapse"
        fixed = CaptureTask(action_type, target_duration_s=params["duration_s"], stop_supported=True,
                            ownership_mode=enum_for("device_activities.ownership_mode").BASELINE_COMPARISON,
                            output_scope=definitions.output_scope_for(driver))
        if action_type == "camera_timelapse":
            fixed = replace(fixed, duration_based=True, wait_after_send=True, end_control=EndControl.DEVICE,
                            start_return_meaning=StartReturn.SENT, completion_mode=CompletionMode.TIME_AND_OUTPUTS,
                            result_wait_margin_s=0)
        return fixed if baseline else replace(fixed, ownership_mode=None, output_scope=None)
    capabilities = [replace(cap, task_factory=task) for cap in definitions.candidate_capabilities(driver)]
    if duration_default is not None:
        record = next(cap for cap in capabilities if cap.action_type == "camera_record")
        schema = deepcopy(record.schema)
        schema["required"].remove("duration_s")
        capabilities = [replace(cap, schema=schema, defaults={"duration_s": duration_default})
                        if cap is record else cap for cap in capabilities]
    config = load_config({"devices": {device_id: {"kind": "camera", "driver": driver}}}, ConfigDefaults())
    return build_catalog(config, DriverDefinitions({driver: DriverDefinition(driver,
        {cap.action_type: (cap,) for cap in capabilities})}))


def _params(driver, action_type):
    if action_type == "camera_timelapse":
        return {"type": "action6_timelapse" if driver == "dji-action6" else "osmo360ii_timelapse",
                "interval_s": 8 if driver == "dji-action6" else 30,
                "duration_s": 1800 if driver == "dji-action6" else 600,
                "outputs": "video", "exposure": {"mode": "auto"} if driver == "dji-action6" else {"mode": "manual", "iso": 800}}
    if driver == "dji-action6":
        return {"type": "action6_record", "duration_s": 10, "resolution": "8k30", "fov": "wide",
                "stabilization": "off", "aperture": "f2.8", "bitrate": "high",
                "exposure": {"mode": "manual", "iso": 800}}
    return {"type": "osmo360ii_record", "duration_s": 10, "exposure": {"mode": "auto", "compensation_ev": 0}}


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_describe_input_and_admitted_facts_match(environment, tmp_path, driver, action_type):
    connection, context = environment
    catalog = _catalog(driver)
    context = replace(context, catalog=catalog)
    body = _plan_body()
    body["actions"][0].update(type=action_type, params=_params(driver, action_type))
    described = next(action for action in catalog.describe_document()["devices"][0]["actions"]
                     if action["type"] == action_type)["parameter_types"][0]
    assert described["type"] == body["actions"][0]["params"]["type"]
    outcome = await _accept((connection, context), tmp_path, body)
    assert outcome.plan_disposition is PlanDisposition.REGISTERED
    row = _action_facts(connection).tables["actions"][1]
    assert parse_exact_json(row["effective_params_json"]) == body["actions"][0]["params"]
    spec = read_action_spec(row)
    assert spec["target_duration_ms"] == body["actions"][0]["params"]["duration_s"] * 1000
    assert spec["ownership_mode"] == 2 and spec["output_scope"] == definitions.output_scope_for(driver)
    repository = HistoryRepository(tmp_path / "state.db")
    historical = repository.restore_entity("action", 1, repository.current_boundary())[("actions", 1)]
    assert read_action_spec(historical) == spec


async def test_resend_preserves_first_defaults_and_scope(environment, tmp_path):
    connection, context = environment
    body = _plan_body()
    params = _params("dji-action6", "camera_record")
    del params["duration_s"]
    body["actions"][0].update(type="camera_record", params=params)
    await _accept((connection, replace(context, catalog=_catalog("dji-action6", duration_default=10))), tmp_path, body)
    before = _action_facts(connection).tables["actions"][1]
    reused = await _accept((connection, replace(context, catalog=_catalog("dji-action6", duration_default=20))),
                           tmp_path, {"request_id": "42"})
    assert reused.plan_disposition is PlanDisposition.REUSED
    after = _action_facts(connection).tables["actions"][1]
    assert read_action_spec(after) == read_action_spec(before)
    assert parse_exact_json(after["effective_params_json"])["duration_s"] == 10


async def test_explicit_null_does_not_become_default_duration(environment, tmp_path):
    connection, context = environment
    body = _plan_body()
    body["actions"][0].update(type="camera_record", params={**_params("dji-action6", "camera_record"), "duration_s": None})
    await _accept((connection, replace(context, catalog=_catalog("dji-action6", duration_default=10))), tmp_path, body)
    row = _action_facts(connection).tables["actions"][1]
    assert row["status"] == enum_for("actions.status").FAILED
    assert read_action_spec(row) is None
    assert connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0] == 0
