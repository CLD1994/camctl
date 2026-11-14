"""已保存动作事实的原输入和公开投影解释。"""
import pytest

from camctl.acceptance.definitions import read_action_spec
from camctl.contracts.input_fields import reconstruct_action_input
from camctl.contracts.public_projection import ProjectionInput, PublicProjectionError, project_public
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import action_error_id
from .helpers import admitted_action_row


def facts(row):
    return ProjectionInput(entity="action", root_id=1, tables={"actions": {1: row}})


def test_duplicate_raw_and_column_is_state_error():
    row = admitted_action_row()
    assert reconstruct_action_input(row)["device_id"] == "cam-1"
    assert read_action_spec(row) == {}
    assert project_public(facts(row))["status"] == "pending"
    row["input_fields_json"] = '{"params":{"type":"single_shot"},"policy":{"max_delay_ms":1000},"device_id":"cam-2"}'
    with pytest.raises(PublicProjectionError):
        project_public(facts(row))


def test_public_projection_rejects_wrong_registered_error_details():
    row = admitted_action_row()
    row.update(status=4, execution_started=0, error_code=action_error_id("action_validation_failed"),
        error_details_json={"issues": [{"field": "actions[0].params.shots", "reason": "range", "value": 99}]},
        execution_spec_json=None, effective_params_json=None, driver_id=None)
    row["input_fields_json"]["params"]["shots"] = 99
    assert reconstruct_action_input(row)["params"]["shots"] == 99
    assert read_action_spec(row) is None
    assert project_public(facts(row))["status"] == "failed"
    row["error_details_json"] = '{"x":1}'
    with pytest.raises(PublicProjectionError):
        project_public(facts(row))


@pytest.mark.parametrize("status", [1, 4])
@pytest.mark.parametrize("missing", ["params", "policy", "device_id", "scheduled_at", "max_delay_ms"])
def test_admitted_input_requires_its_original_basis(missing, status):
    row = admitted_action_row()
    if status == 4:
        row.update(status=4, execution_started=1, error_code=action_error_id("device_start_failed"), error_details_json={})
    assert reconstruct_action_input(row)["params"] == {"type": "single_shot"}
    assert read_action_spec(row) == {}
    assert project_public(facts(row))["status"] == ("pending" if status == 1 else "failed")
    if missing in {"params", "policy"}:
        del row["input_fields_json"][missing]
    else:
        row[missing] = None
    with pytest.raises(ConsistencyError):
        reconstruct_action_input(row)
    with pytest.raises(ConsistencyError):
        read_action_spec(row)
    with pytest.raises(PublicProjectionError):
        project_public(facts(row))


@pytest.mark.parametrize("action_type,params", [
    ("obtain_action_outputs", {"source": {"current_plan": True}, "purpose": "manual"}),
    ("delete_action_outputs", {"output_ids": ["500"]}),
    ("cancel_task", {"target": {"plan_instance_id": "500"}}),
    ("report_status", {"scope": "full"}),
])
def test_admitted_non_capture_params_keep_their_required_structure(action_type, params):
    row = admitted_action_row(action_type, params)
    row.update(status=6, cancel_requested=1)
    original = {"name": "control", "type": action_type, "params": params, "scheduled_at": "2026-01-15 09:00:00"}
    assert reconstruct_action_input(row) == original
    assert read_action_spec(row) == ({"selection_mode": 1} if action_type == "obtain_action_outputs" else {})
    fragment = project_public(facts(row))
    assert fragment["status"] == "canceled" and "error" not in fragment
    row["input_fields_json"] = {"params": {}}
    with pytest.raises(ConsistencyError):
        reconstruct_action_input(row)
    with pytest.raises(ConsistencyError):
        read_action_spec(row)
    with pytest.raises(PublicProjectionError):
        project_public(facts(row))
