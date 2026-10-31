"""可用性失败的公共登记与当前数据库结构。"""

import json
import sqlite3

import pytest

from camctl.contracts.workflow_errors import (
    action_error_id,
    item_error_id,
    validate_public_error,
)
from camctl.resources import resource_bytes


DETAILS = {"device_id": "cam", "operation_run_id": "42", "attempts_used": 3, "max_attempts": 2}


@pytest.mark.parametrize("code,stage", [
    ("device_availability_unconfirmed", "execution"),
    ("source_device_unavailable", "source_read"),
])
def test_availability_error_requires_actual_check_facts(code, stage):
    validate_public_error({"code": code, "stage": stage, "details": DETAILS})
    for missing in DETAILS:
        with pytest.raises(ValueError):
            validate_public_error({"code": code, "stage": stage,
                                   "details": {k: v for k, v in DETAILS.items() if k != missing}})
    with pytest.raises(ValueError):
        validate_public_error({"code": code, "stage": "admission", "details": DETAILS})
    for field, invalid in (("device_id", ""), ("operation_run_id", "0"),
                           ("attempts_used", -1), ("max_attempts", True), ("max_attempts", 0)):
        with pytest.raises(ValueError):
            validate_public_error({"code": code, "stage": stage,
                                   "details": {**DETAILS, field: invalid}})


@pytest.mark.parametrize("reason", ["physical_identity_mismatch", "physical_identity_conflict"])
def test_physical_identity_binding_failure_is_registered(reason):
    validate_public_error({"code": "device_binding_unavailable", "stage": "execution", "details": {
        "device_id": "cam", "expected_driver_id": "hasselblad_cfv100c",
        "reason": reason,
    }})


def test_unstarted_accepted_photo_can_store_availability_failure():
    connection = sqlite3.connect(":memory:")
    connection.executescript(resource_bytes("sql/core.sql").decode())
    code = action_error_id("device_availability_unconfirmed")
    values = (1, 1, 0, "photo", 1, "cam", 0, "{}", "{}", "driver", 0,
              "{}", 4, 0, 0, code, json.dumps(DETAILS), 1, 1, 1)
    sql = ("INSERT INTO actions (id,plan_id,input_index,name,type,device_id,scheduled_at,"
           "input_fields_json,effective_params_json,driver_id,max_delay_ms,execution_spec_json,"
           "status,execution_started,cancel_requested,error_code,error_details_json,"
           "created_event_id,last_event_id,change_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    connection.execute(sql, values)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE actions SET execution_started=1")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE actions SET type=4,device_id=NULL,driver_id=NULL,effective_params_json=NULL,max_delay_ms=NULL")


def test_availability_source_error_has_item_number():
    connection = sqlite3.connect(":memory:")
    connection.executescript(resource_bytes("sql/workflows.sql").decode())
    code = item_error_id("obtain_items", "source_device_unavailable")
    connection.execute(
        "INSERT INTO obtain_items (id,selection_id,requested_output_id,output_id,basis,status,"
        "source_dependency,error_code,error_details_json) VALUES (1,1,9,9,5,4,0,?,?)",
        (code, json.dumps(DETAILS)),
    )
    assert connection.execute("SELECT error_code FROM obtain_items").fetchone() == (code,)
