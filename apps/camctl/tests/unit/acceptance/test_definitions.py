"""固定定义读取验证保存事实，不接触设备或规则资源。"""
from enum import IntEnum

import pytest

from camctl.acceptance import definitions
from camctl.contracts.values import ConsistencyError


class ActionType(IntEnum):
    CAMERA_TAKE_PHOTO = 1


@pytest.fixture(autouse=True)
def row_contract(monkeypatch):
    monkeypatch.setattr(definitions, "decode_member", lambda column,value:ActionType(value))
    monkeypatch.setattr(definitions, "reconstruct_action_input", lambda row:{"params":{"type":"single_shot"}})


def _row(**changes):
    return {"type":1,"name":"shoot", "execution_spec_json":{}, "error_code":None,
        "input_fields_json":{"params":{"type":"single_shot"}, "policy":{"max_delay_ms":1000}},
        "device_id":"cam-1", "scheduled_at":0, "group_name":None, "max_delay_ms":1000,
        "effective_params_json":{"type":"single_shot"},"driver_id":"driver-a", **changes}


@pytest.mark.parametrize("effective", [{}, {"type":None}, {"type":""}, [], "null", 1])
def test_reader_rejects_missing_effective_parameter_identity(effective):
    with pytest.raises(ConsistencyError):
        definitions.read_action_spec(_row(effective_params_json=effective))


@pytest.mark.parametrize("driver", ["", None, 1, False])
def test_reader_rejects_missing_driver_identity(driver):
    with pytest.raises(ConsistencyError):
        definitions.read_action_spec(_row(driver_id=driver))


def test_reader_decodes_effective_json_without_recomputing_it():
    assert definitions.read_action_spec(_row(effective_params_json='{"type":"single_shot"}')) == {}


def test_reader_rejects_changed_effective_parameter_type():
    with pytest.raises(ConsistencyError):
        definitions.read_action_spec(_row(effective_params_json={"type":"other"}))
