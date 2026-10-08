"""固定执行定义经受理、历史读取和重送保持不变。"""
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.acceptance.definitions import read_action_spec
from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import AcceptanceStateError, PlanDisposition
from camctl.contracts.values import ConsistencyError
from camctl.devices.tasks import CaptureTask, CompletionMode, EndControl, StartReturn
from camctl.persistence.repositories.history import HistoryRepository
from .test_acceptance import Catalog, CAMERA_DEFINITION, _accept, _plan_body, environment
from .test_fields import _action_facts

pytestmark = pytest.mark.asyncio


class TaskCatalog(Catalog):
    duration = Decimal("1.234")

    def parameter_definition(self, device_id, action_type, parameter_type):
        if not self.device_exists(device_id) or parameter_type != "single_shot":
            return None
        def task(params):
            if action_type == "camera_record":
                return CaptureTask(action_type, target_duration_s=self.duration, stop_supported=True)
            return CaptureTask(action_type, target_duration_s=self.duration, duration_based=True,
                wait_after_send=True, stop_supported=False, end_control=EndControl.DEVICE,
                start_return_meaning=StartReturn.SENT, completion_mode=CompletionMode.TIME_AND_OUTPUTS,
                result_wait_margin_s=Decimal("0.005"))
        return ParameterDefinition(CAMERA_DEFINITION, {"shots":1}, True, task)


def _obtain(params):
    return {"name":"fetch", "type":"obtain_action_outputs", "scheduled_at":"2026-01-15 09:00:00", "params":params}


@pytest.mark.parametrize("action_type, expected", [
    ("camera_take_photo", {}), ("camera_record", {"target_duration_ms":1234}),
    ("camera_timelapse", {"target_duration_ms":1234, "duration_based":True, "wait_after_send":True,
        "stop_supported":False,"end_control":1,"start_return_meaning":1,"completion_mode":2,"result_wait_margin_ms":5}),
])
async def test_capture_specs_match_owned_contracts(environment, tmp_path, action_type, expected):
    connection, context = environment
    context = replace(context, catalog=TaskCatalog())
    body = _plan_body()
    body["actions"][0]["type"] = action_type
    await _accept((connection, context), tmp_path, body)
    row = _action_facts(connection).tables["actions"][1]
    assert read_action_spec(row) == expected
    assert row["execution_spec_json"] is not None


@pytest.mark.parametrize("params, expected", [
    ({"source":{"current_plan":True},"purpose":"manual"}, 1),
    ({"source":{"current_plan":True},"purpose":"manual","filter":"default"}, 1),
    ({"source":{"action_name":"shoot"},"purpose":"manual","filter":"preview"}, 2),
    ({"source":{"action_name":"shoot"},"purpose":"auto_preview","filter":"preview"}, 2),
    ({"source":{"action_instance_id":"500"},"purpose":"manual","output_ids":["900"]}, 3),
])
async def test_obtain_selection_fixed_without_selecting_files(environment, tmp_path, params, expected):
    connection, context = environment
    context = replace(context, catalog=TaskCatalog())
    body = _plan_body()
    body["actions"].append(_obtain(params))
    await _accept((connection, context), tmp_path, body)
    row = _action_facts(connection, 2).tables["actions"][2]
    assert read_action_spec(row) == {"selection_mode":expected}
    assert row["effective_params_json"] is None and row["driver_id"] is None
    assert connection.execute("SELECT COUNT(*) FROM obtain_items").fetchone() == (0,)


@pytest.mark.parametrize("action_type, params", [
    ("delete_action_outputs", {"output_ids":["500"]}),
    ("cancel_task", {"target":{"plan_instance_id":"500"}}),
    ("report_status", {"scope":"full"}),
])
async def test_non_capture_spec_is_valid_empty_object(environment, tmp_path, action_type, params):
    connection, _ = environment
    action = {"name":"control", "type":action_type, "params":params, "scheduled_at":"2026-01-15 09:00:00"}
    await _accept(environment, tmp_path, _plan_body(actions=[action]))
    row = _action_facts(connection).tables["actions"][1]
    assert read_action_spec(row) == {}
    assert row["target_selection_state"] == (1 if action_type in {"delete_action_outputs","cancel_task"} else None)


async def test_failed_admission_keeps_sql_null_spec(environment, tmp_path):
    connection, _ = environment
    await _accept(environment, tmp_path, _plan_body(actions=[{"name":"bad","type":"camera_record"}]))
    row = _action_facts(connection).tables["actions"][1]
    assert row["execution_spec_json"] is None
    assert read_action_spec(row) is None


async def test_valid_input_with_invalid_task_rolls_back_whole_registration(environment, tmp_path):
    connection, context = environment
    catalog = TaskCatalog()
    catalog.duration = Decimal("1.0005")
    body = _plan_body()
    body["actions"][0]["type"] = "camera_record"
    with pytest.raises(AcceptanceStateError):
        await _accept((connection, replace(context, catalog=catalog)), tmp_path, body)
    assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)
    assert connection.execute("SELECT COUNT(*) FROM actions").fetchone() == (0,)


async def test_history_and_retry_keep_first_definition_after_default_changes(environment, tmp_path):
    connection, context = environment
    catalog = TaskCatalog()
    context = replace(context, catalog=catalog)
    body = _plan_body()
    body["actions"][0]["type"] = "camera_record"
    await _accept((connection, context), tmp_path, body)
    (tmp_path / "plan.json").unlink()
    catalog.duration = Decimal("9.876")
    reused = await _accept((connection, context), tmp_path, {"request_id":"42"})
    assert reused.plan_disposition is PlanDisposition.REUSED
    repository = HistoryRepository(tmp_path / "state.db")
    row = repository.restore_entity("action", 1, repository.current_boundary())[("actions", 1)]
    assert read_action_spec(row) == {"target_duration_ms":1234}


@pytest.mark.parametrize("spec", [None, "null", "[]", "{}", '{"target_duration_ms":true}', '{"target_duration_ms":0}', '{"action_type":"camera_record","target_duration_ms":1}'])
async def test_saved_record_definition_cannot_be_recomputed(environment, tmp_path, spec):
    connection, context = environment
    body = _plan_body()
    body["actions"][0]["type"] = "camera_record"
    await _accept((connection, replace(context, catalog=TaskCatalog())), tmp_path, body)
    row = _action_facts(connection).tables["actions"][1]
    with pytest.raises(ConsistencyError):
        read_action_spec({**row, "execution_spec_json":spec})


async def test_real_history_replay_and_reopened_database_keep_first_facts(environment, tmp_path):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import accept_input
    from camctl.contracts.history_values import INITIAL_BOUNDARY, ReadScope, ReadOrder
    from camctl.contracts.input_fields import reconstruct_action_input
    from camctl.contracts.values import new_operation_key
    from camctl.history.events import event_type_name, branch_of
    from camctl.history.replay import EntityImage, RestoreSeed, restore
    from camctl.history.validators import ValidatedEvent
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from .test_acceptance import _owned

    connection, context = environment
    catalog = TaskCatalog()
    definition = catalog.parameter_definition
    catalog.parameter_definition = lambda *args: replace(definition(*args), schema={**CAMERA_DEFINITION,
        "properties":{**CAMERA_DEFINITION["properties"], "fraction":{"type":"number"}}})
    context = replace(context, catalog=catalog)
    body = _plan_body()
    body["actions"][0]["type"] = "camera_record"
    body["actions"][0]["params"]["fraction"] = Decimal("0.12345678901234567890123456789")
    body["actions"].extend([
        _obtain({"source":{"current_plan":True},"purpose":"manual"}),
        {"name":"cleanup","type":"delete_action_outputs","scheduled_at":"2026-01-15 10:00:00","params":{"source":{"current_plan":True}}},
    ])
    await accept_input(ParsedInput("removed.json",body), context,new_operation_key(),_owned(environment))
    repository = HistoryRepository(tmp_path / "state.db")
    boundary = repository.current_boundary()
    scope = ReadScope(order=ReadOrder.ASCENDING, previous_position=None, lower_position=1,
        upper_position=boundary.last_event_id, batch_limit=100, cursor_position=lambda cursor:cursor)
    envelopes = repository.read_events(scope,boundary).items
    validated = []
    for envelope in envelopes:
        references = tuple(connection.execute("SELECT entity_type,entity_id FROM entity_event_links WHERE event_id=? ORDER BY id",(envelope.event_id,)))
        # 此次受理的每条事件归一个已登记对象；归属来自实际历史目录。
        assert len(set(references)) == 1
        owner = references[0]
        validated.append(ValidatedEvent(envelope, event_type_name(envelope.event_type),
            branch_of(envelope.event_type,envelope.reason)[0], tuple(set(references)),
            {(row.table,row.row_id):owner for row in envelope.rows}))
    specs = ({"target_duration_ms":1234}, {"selection_mode":1}, {})
    for index, expected in enumerate(specs,1):
        seed = RestoreSeed(EntityImage(1,index,False,{},0,0), INITIAL_BOUNDARY)
        image = restore(seed,validated,boundary)
        row = image.rows[("actions",index)]
        assert reconstruct_action_input(row) == body["actions"][index-1]
        assert read_action_spec(row) == expected
        if index in {2,3}:
            assert row["source_resolution_state"] == 2 and row["resolved_source_plan_id"] == 1
            members = [member["depends_on_action_id"] for (table,_),member in image.rows.items() if table == "action_dependencies"]
            assert members == [1]
        empty = restore(RestoreSeed(image,boundary),validated,INITIAL_BOUNDARY)
        assert empty.exists is False and not empty.rows and empty.change_count == 0
    connection.close()
    reopened = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW,DbConfig())
    try:
        catalog.duration = Decimal("9.876")
        reused = await accept_input(ParsedInput("retry.json",{"request_id":"42"}),context,new_operation_key(),reopened)
        assert reused.plan_disposition is PlanDisposition.REUSED
        saved = repository.restore_entity("action", 1, repository.current_boundary())[("actions", 1)]
        assert read_action_spec(saved) == {"target_duration_ms":1234}
        assert reconstruct_action_input(saved)["params"]["fraction"] == Decimal("0.12345678901234567890123456789")
    finally:
        reopened.connection.close()
