"""分层受理及原始字段唯一来源的真实组件组合。"""
from copy import deepcopy

import pytest

from camctl.acceptance.service import PlanDisposition
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.public_projection import ProjectionInput, project_public
from camctl.contracts.schemas import create_validator, validation_errors
from camctl.contracts.workflow_errors import _registry
from .test_acceptance import environment, _accept, _plan_body

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("field,value", [
    ("params", None), ("params", []), ("scheduled_at", None), ("scheduled_at", 9),
    ("scheduled_at", "2026-02-30 09:00:00"), ("device_id", {}), ("device_id", None),
    ("group", " bad "), ("policy", {"max_delay_ms": True}), ("extra", 17),
])
async def test_action_fields_do_not_reject_plan(environment, tmp_path, field, value):
    connection, _ = environment
    bad = deepcopy(_plan_body()["actions"][0])
    bad["name"] = "bad"
    bad[field] = value
    body = _plan_body(actions=[bad, _plan_body()["actions"][0]])
    result = await _accept(environment, tmp_path, body)
    assert result.plan_disposition is PlanDisposition.REGISTERED
    assert connection.execute("SELECT name,status FROM actions ORDER BY input_index").fetchall() == [("bad",4),("shoot",1)]


async def test_missing_action_fields_preserved(environment, tmp_path):
    connection, _ = environment
    body = _plan_body(actions=[{"name":"bad", "type":"camera_take_photo"}])
    result = await _accept(environment, tmp_path, body)
    assert result.plan_disposition is PlanDisposition.REGISTERED
    fields, details = connection.execute("SELECT input_fields_json,error_details_json FROM actions").fetchone()
    assert parse_exact_json(fields) == {}
    assert {i["field"] for i in parse_exact_json(details)["issues"]} == {
        "actions[0].device_id", "actions[0].scheduled_at", "actions[0].params", "actions[0].policy",
    }
    assert all("value" not in i for i in parse_exact_json(details)["issues"])


async def test_valid_policy_survives_other_failure(environment, tmp_path):
    connection, _ = environment
    body = _plan_body()
    body["actions"][0]["params"]["shots"] = 99
    await _accept(environment, tmp_path, body)
    row = connection.execute("SELECT max_delay_ms,input_fields_json,device_id,scheduled_at,group_name FROM actions").fetchone()
    assert row[0] == 1000
    assert parse_exact_json(row[1]) == {"params":{"type":"single_shot","shots":99}, "policy":{"max_delay_ms":1000}}
    assert row[2] == "cam-1" and row[3] == 1768467600000000


async def test_invalid_date_keeps_raw_exception(environment, tmp_path):
    connection, _ = environment
    body = _plan_body()
    body["actions"][0]["scheduled_at"] = "2026-02-30 09:00:00"
    await _accept(environment, tmp_path, body)
    row = connection.execute("SELECT scheduled_at,input_fields_json FROM actions").fetchone()
    assert row[0] is None
    assert parse_exact_json(row[1])["scheduled_at"] == "2026-02-30 09:00:00"


async def test_admission_details_match_registry(environment, tmp_path):
    connection, _ = environment
    body = _plan_body()
    body["actions"][0]["params"] = {"type":"single_shot","shots":99}
    await _accept(environment, tmp_path, body)
    details = parse_exact_json(connection.execute("SELECT error_details_json FROM actions").fetchone()[0])
    assert details["issues"] == [{"field":"actions[0].params.shots", "reason":"range", "value":99}]
    schema = {"$schema":"https://json-schema.org/draft/2020-12/schema", **_registry()["codes"]["action_validation_failed"]["details_schema"]}
    assert not validation_errors(create_validator(schema), details)


def _action_facts(connection, action_id=1):
    cursor = connection.execute("SELECT * FROM actions WHERE id=?", (action_id,))
    row = dict(zip([d[0] for d in cursor.description], cursor.fetchone()))
    return ProjectionInput(entity="action", root_id=action_id, tables={"actions":{action_id: row}})


@pytest.mark.parametrize("field", ["device_id", "scheduled_at", "group", "params", "policy"])
async def test_public_projection_preserves_explicit_null(environment, tmp_path, field):
    connection, _ = environment
    body = _plan_body()
    body["actions"][0][field] = None
    await _accept(environment, tmp_path, body)
    fragment = project_public(_action_facts(connection))
    public_field = "input_params" if field == "params" else field
    assert public_field in fragment
    assert fragment[public_field] is None


async def test_duplicate_raw_and_column_is_state_error(environment, tmp_path):
    from camctl.contracts.public_projection import PublicProjectionError
    connection, _ = environment
    await _accept(environment, tmp_path, _plan_body())
    facts = _action_facts(connection)
    facts.tables["actions"][1]["input_fields_json"] = '{"device_id":"cam-2"}'
    with pytest.raises(PublicProjectionError):
        project_public(facts)


@pytest.mark.parametrize("changes", [
    {}, {"params":None}, {"device_id":None}, {"group":None},
    {"scheduled_at":"2026-02-30 09:00:00"}, {"extra":{"n":1}},
])
async def test_original_action_round_trip(environment, tmp_path, changes):
    from camctl.contracts import input_fields
    connection, _ = environment
    original = {**_plan_body()["actions"][0], **changes}
    await _accept(environment, tmp_path, _plan_body(actions=[original]))
    facts = _action_facts(connection)
    assert input_fields.reconstruct_action_input(facts.tables["actions"][1]) == original


@pytest.mark.parametrize("action_type", ["camera_take_photo","camera_record","camera_timelapse","obtain_action_outputs","delete_action_outputs","cancel_task","report_status"])
async def test_invalid_calendar_is_local_failure_for_every_action(environment, tmp_path, action_type):
    connection, _ = environment
    params = {"type":"single_shot"} if action_type.startswith("camera_") else {
        "obtain_action_outputs":{"source":{"current_plan":True},"purpose":"manual"},
        "delete_action_outputs":{"output_ids":["900"]}, "cancel_task":{"target":{"plan_instance_id":"500"}},
        "report_status":{"scope":"full"},
    }[action_type]
    bad = {"name":"bad", "type":action_type, "params":params, "scheduled_at":"2026-02-30 09:00:00"}
    if action_type.startswith("camera_"):
        bad.update(device_id="cam-1", policy={"max_delay_ms":1000})
    accepted = await _accept(environment, tmp_path, _plan_body(actions=[bad, _plan_body()["actions"][0]]))
    assert accepted.plan_disposition is PlanDisposition.REGISTERED
    row = _action_facts(connection).tables["actions"][1]
    assert row["status"] == 4 and row["scheduled_at"] is None
    assert row["execution_spec_json"] is None
    assert parse_exact_json(row["input_fields_json"])["scheduled_at"] == bad["scheduled_at"]


@pytest.mark.parametrize("action_type,params", [("cancel_task",{"target":{"plan_instance_id":"500"}}), ("report_status",{"scope":"full"})])
async def test_optional_schedule_and_group_are_preserved(environment, tmp_path, action_type, params):
    connection, _ = environment
    original = {"name":"control", "type":action_type, "params":params, "group":"control-group", "scheduled_at":"2026-01-15 09:00:00"}
    await _accept(environment, tmp_path, _plan_body(actions=[original]))
    row = _action_facts(connection).tables["actions"][1]
    from camctl.contracts.input_fields import reconstruct_action_input
    assert row["status"] == 1 and row["group_name"] == "control-group"
    assert row["scheduled_at"] == 1768467600000000
    assert reconstruct_action_input(row) == original


async def test_history_keeps_exact_original_number(environment, tmp_path):
    from decimal import Decimal
    from camctl.persistence.repositories.history import HistoryRepository
    connection, _ = environment
    original = _plan_body()["actions"][0]
    original["extra"] = {"precise":9007199254740993, "fraction":Decimal("0.12345678901234567890123456789")}
    # 直接使用解析后输入端口，避免测试写文件时先经二进制浮点数。
    from .test_atomicity import _process
    from camctl.persistence.models import DbOutcomeKind
    assert _process(_plan_body(actions=[original]), connection).kind is DbOutcomeKind.COMPLETED
    row = HistoryRepository(tmp_path / "state.db").entity_facts(1,(1,))["actions"][1]
    from camctl.contracts.input_fields import reconstruct_action_input
    assert reconstruct_action_input(row) == original


@pytest.mark.parametrize("space", [" ", "\u00a0", "\u2003"])
@pytest.mark.parametrize("owner,field", [("plan","name"),("action","name"),("action","group"),("source","action_name"),("source","group"),("target","group")])
async def test_unicode_edge_whitespace_is_not_normalized(environment, tmp_path, space, owner, field):
    connection, _ = environment
    body = _plan_body()
    if owner == "plan":
        body[field] = space + "plan"
    elif owner == "action":
        body["actions"][0][field] = "shoot" + space
    elif owner == "source":
        body["actions"].append({"name":"fetch","type":"obtain_action_outputs","scheduled_at":"2026-01-15 09:00:00","params":{"source":{field:space+"shoot"},"purpose":"manual"}})
    else:
        body["actions"].append({"name":"cancel","type":"cancel_task","params":{"target":{"plan_instance_id":"500","group":"shoot"+space}}})
    result = await _accept(environment, tmp_path, body)
    if owner == "plan" or (owner == "action" and field == "name"):
        assert result.plan_disposition is PlanDisposition.REJECTED
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)
    else:
        assert result.plan_disposition is PlanDisposition.REGISTERED
        row = _action_facts(connection, 1 if owner == "action" else 2).tables["actions"][1 if owner == "action" else 2]
        assert row["status"] == 4 and row["execution_spec_json"] is None


async def test_history_rejects_invalid_json_instead_of_returning_text(environment, tmp_path):
    from camctl.persistence.repositories.history import HistoryRepository
    from camctl.contracts.values import ConsistencyError
    connection, _ = environment
    await _accept(environment, tmp_path, _plan_body())
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("UPDATE actions SET input_fields_json = '{broken' WHERE id=1")
    with pytest.raises(ConsistencyError):
        HistoryRepository(tmp_path / "state.db").entity_facts(1,(1,))


async def test_public_projection_rejects_wrong_registered_error_details(environment, tmp_path):
    from camctl.contracts.public_projection import PublicProjectionError
    connection, _ = environment
    original = _plan_body()["actions"][0]
    original["params"]["shots"] = 99
    await _accept(environment, tmp_path, _plan_body(actions=[original]))
    facts = _action_facts(connection)
    facts.tables["actions"][1]["error_details_json"] = '{"x":1}'
    with pytest.raises(PublicProjectionError):
        project_public(facts)


@pytest.mark.parametrize("status", [1, 4])
@pytest.mark.parametrize("missing", ["params", "policy", "device_id", "scheduled_at", "max_delay_ms"])
async def test_admitted_input_requires_its_original_basis(environment, tmp_path, missing, status):
    from camctl.acceptance.definitions import read_action_spec
    from camctl.contracts.input_fields import reconstruct_action_input
    from camctl.contracts.public_projection import PublicProjectionError
    from camctl.contracts.values import ConsistencyError
    connection, _ = environment
    await _accept(environment, tmp_path, _plan_body())
    facts = _action_facts(connection)
    row = facts.tables["actions"][1]
    row["status"] = status
    if status == 4:
        from camctl.contracts.workflow_errors import action_error_id
        row.update(execution_started=1, error_code=action_error_id("device_start_failed"), error_details_json={})
    assert project_public(facts)["status"] == ("pending" if status == 1 else "failed")
    if missing in {"params", "policy"}:
        fields = parse_exact_json(row["input_fields_json"])
        del fields[missing]
        row["input_fields_json"] = fields
    else:
        row[missing] = None
    with pytest.raises(ConsistencyError):
        reconstruct_action_input(row)
    with pytest.raises(ConsistencyError):
        read_action_spec(row)
    with pytest.raises(PublicProjectionError):
        project_public(facts)


async def test_history_reader_rejects_missing_admitted_input(environment, tmp_path):
    from camctl.persistence.repositories.history import HistoryRepository
    from camctl.contracts.values import ConsistencyError
    connection, _ = environment
    await _accept(environment, tmp_path, _plan_body())
    connection.execute("UPDATE actions SET input_fields_json='{}' WHERE id=1")
    with pytest.raises(ConsistencyError):
        HistoryRepository(tmp_path / "state.db").entity_facts(1, (1,))


@pytest.mark.parametrize("action_type,params", [
    ("obtain_action_outputs", {"source":{"current_plan":True},"purpose":"manual"}),
    ("delete_action_outputs", {"output_ids":["500"]}),
    ("cancel_task", {"target":{"plan_instance_id":"500"}}),
    ("report_status", {"scope":"full"}),
])
async def test_admitted_non_capture_params_keep_their_required_structure(environment, tmp_path, action_type, params):
    from camctl.acceptance.definitions import read_action_spec
    from camctl.contracts.input_fields import reconstruct_action_input
    from camctl.contracts.public_projection import PublicProjectionError
    from camctl.contracts.values import ConsistencyError
    connection, _ = environment
    original = {"name":"control","type":action_type,"params":params,"scheduled_at":"2026-01-15 09:00:00"}
    await _accept(environment, tmp_path, _plan_body(actions=[original]))
    facts = _action_facts(connection)
    row = facts.tables["actions"][1]
    row.update(status=6, cancel_requested=1)
    assert reconstruct_action_input(row) == original
    assert read_action_spec(row) is not None
    fragment = project_public(facts)
    assert fragment["status"] == "canceled" and "error" not in fragment
    row["input_fields_json"] = {"params":{}}
    with pytest.raises(ConsistencyError):
        reconstruct_action_input(row)
    with pytest.raises(ConsistencyError):
        read_action_spec(row)
    with pytest.raises(PublicProjectionError):
        project_public(facts)
