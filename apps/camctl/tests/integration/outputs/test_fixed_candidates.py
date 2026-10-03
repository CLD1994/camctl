"""来源、选择和目标固定后，候选判断遵守各记录的实际生命周期。"""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.history_values import TransactionRange
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.outputs.competition import has_product_predecessor
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs

from .test_product_competition import competition_database, _proposal
from .test_qualification import (
    qualification_environment, _NOW, _Seedling, _candidate,
    _seed_device_file, _seed_output, _seed_plan,
)


def _fixed_source(connection, *, status=1, selection=None, mode=1, requested=("701",)):
    params = {"source": {"action_name": "action-11"}}
    if mode == 2:
        params["filter"] = "preview"
    elif mode == 3:
        params["output_ids"] = list(requested)
    connection.execute("UPDATE actions SET plan_id=2,status=?,execution_started=?,source_resolution_state=2,"
        " resolved_source_plan_id=2,input_fields_json=?,execution_spec_json=? WHERE id=91",
        (status, int(status != 1), json.dumps({"params": params}), json.dumps({"selection_mode": mode})))
    connection.execute("INSERT INTO action_dependencies (id,action_id,depends_on_action_id) VALUES (191,91,11)")
    if selection is not None:
        connection.execute("INSERT INTO obtain_source_selections (id,dependency_id,status) VALUES (191,191,?)", (selection,))
    connection.commit()


@pytest.mark.parametrize("mode,requested,blocked", [(1, (), True), (2, (), False),
                                                  (3, ("701",), True), (3, ("999",), False)])
def test_pending_fixed_source_competes_without_initializing_selection(competition_database, mode, requested, blocked):
    owned = competition_database
    _fixed_source(owned.connection, mode=mode, requested=requested)
    before = tuple(owned.connection.iterdump())
    result = outputs.OutputsRepository().grant_file(_candidate(_Seedling(31, 101, 701, 501)),
                                                   new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.reason == ("business_order" if blocked else None)
    assert owned.connection.execute("SELECT COUNT(*) FROM obtain_source_selections WHERE dependency_id=191").fetchone() == (0,)
    if blocked:
        assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("status,selection", [(1, 1), (1, 2), (2, None)])
def test_selection_initialization_must_match_action_phase(competition_database, status, selection):
    owned = competition_database
    _fixed_source(owned.connection, status=status, selection=selection)
    before = tuple(owned.connection.iterdump())
    result = outputs.OutputsRepository().grant_file(_candidate(_Seedling(31, 101, 701, 501)),
                                                   new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


def test_pending_selection_cannot_already_have_items(competition_database):
    connection = competition_database.connection
    _fixed_source(connection, status=2, selection=1)
    _selected_item(connection)
    connection.commit()
    before = tuple(connection.iterdump())
    result = outputs.OutputsRepository().grant_file(_candidate(_Seedling(31, 101, 701, 501)),
                                                   new_operation_key(), competition_database)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(connection.iterdump()) == before


def test_formal_grant_requires_reliable_empty_prestart_selection(competition_database):
    _fixed_source(competition_database.connection, mode=2)
    event, context = _proposal(competition_database)
    assert validate_event(event, context).branch_name == "GRANT"
    assert context.complete_rows("obtain_source_selections", "dependency_id", 191) == {}
    ranges = dict(context.read_coverage.ranges)
    ranges["obtain_source_selections", "dependency_id"] -= {191}
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, read_coverage=ReadCoverage(ranges)))


def test_formal_grant_sees_prestart_candidate_before_selection_exists(competition_database):
    _fixed_source(competition_database.connection, mode=2)
    event, context = _proposal(competition_database)
    future = deepcopy(context.state_rows)
    candidate = context.state_rows["actions"][91]
    candidate["input_fields_json"]["params"].pop("filter")
    candidate["execution_spec_json"] = {"selection_mode": 1}
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, transaction_rows=future))


@pytest.mark.parametrize("status,selection_states", [(1, (1,)), (1, (2,)), (2, ()), (2, (1, 1))])
def test_formal_grant_rejects_inconsistent_selection_initialization(
    competition_database, status, selection_states,
):
    _fixed_source(competition_database.connection, status=2, selection=1, mode=2)
    event, context = _proposal(competition_database)
    assert validate_event(event, context).branch_name == "GRANT"
    context.state_rows["actions"][91].update(status=status, execution_started=int(status != 1))
    template = context.state_rows["obtain_source_selections"].pop(191)
    for identity, state in enumerate(selection_states, start=191):
        context.state_rows["obtain_source_selections"][identity] = dict(template, id=identity, status=state)
    with pytest.raises(EventValidationError):
        validate_event(event, context)


def _assert_predecessor(owned, expected):
    """同一可靠读取结果必须在 SQL 候选判断和事件当前事实判断中一致。"""
    before = tuple(owned.connection.iterdump())
    reads = outputs._ProductReads(owned.connection, {})
    assert has_product_predecessor(reads, 31, 701, _NOW) is expected
    context = EventContext(TransactionRange(2, 2, 2), {}, reads.state_rows,
                           read_coverage=reads.read_coverage())
    assert has_product_predecessor(outputs._CurrentProductReads(context), 31, 701, _NOW) is expected
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("status,selection", [(1, None), (2, 1)])
def test_unfinished_source_does_not_compete(competition_database, status, selection):
    _fixed_source(competition_database.connection, status=status, selection=selection)
    competition_database.connection.execute("UPDATE actions SET status=2 WHERE id=11")
    competition_database.connection.commit()
    _assert_predecessor(competition_database, False)


def _selected_item(connection):
    connection.execute(
        "INSERT INTO obtain_items (id,selection_id,output_id,basis,status,source_dependency)"
        " VALUES (191,191,701,1,2,0)")


@pytest.mark.parametrize("mode", [1, 2], ids=["default", "preview"])
def test_fixed_choice_keeps_target_after_repair_registration(competition_database, mode):
    connection = competition_database.connection
    _fixed_source(connection, status=2, selection=2, mode=mode)
    _selected_item(connection)
    original_id = 701
    if mode == 2:
        original_id = 703
        _seed_device_file(connection, 503, 11)
        _seed_output(connection, original_id, 11, 503)
        connection.execute("UPDATE device_files SET role=3,original_device_file_id=503,"
            " pairing_evidence_json='{\"method\":1,\"observation\":{}}' WHERE id=501")
        connection.execute("UPDATE outputs SET kind=3 WHERE id=701")
        connection.execute("INSERT INTO output_origins (output_id,original_output_id) VALUES (701,703)")
        connection.execute("UPDATE obtain_items SET basis=3,original_output_id=703,preview_output_id=701"
            " WHERE id IN (101,191)")
    connection.commit()
    _assert_predecessor(competition_database, True)
    connection.execute(
        "INSERT INTO intermediate_files (id,owner_action_id,purpose,relative_path,retention_state,"
        " cleanup_state,size_bytes,created_event_id,last_event_id,change_count)"
        " VALUES (599,11,4,'derived/599.mp4',3,1,100,1,1,1)")
    connection.execute(
        "INSERT INTO outputs (id,source_action_id,kind,intermediate_file_id,availability,cleanup_status,"
        " media_json,created_event_id,last_event_id,change_count) VALUES (999,11,2,599,1,1,'{}',1,1,1)")
    connection.execute("INSERT INTO output_origins (output_id,original_output_id) VALUES (999,?)", (original_id,))
    connection.commit()
    _assert_predecessor(competition_database, True)
    # 对照：尚未固定的选择会选中修复成品，因此不再竞争 701。
    connection.execute("DELETE FROM obtain_items WHERE id=191")
    connection.execute("UPDATE obtain_source_selections SET status=1 WHERE id=191")
    connection.commit()
    _assert_predecessor(competition_database, False)


@pytest.mark.parametrize("status,expected", [(1, True), (2, True), (4, False), (5, False)])
def test_fixed_explicit_item_competes_only_while_unfinished(competition_database, status, expected):
    connection = competition_database.connection
    _fixed_source(connection, status=2, selection=2, mode=3)
    connection.execute(
        "INSERT INTO obtain_items (id,selection_id,requested_output_id,output_id,basis,status,"
        " source_dependency,error_code,error_details_json) VALUES (191,191,701,?,5,?,0,?,?)",
        (None if status == 1 else 701, status, 3 if status == 4 else None,
         json.dumps({"output_id": "701", "availability": "missing"}) if status == 4 else None))
    connection.commit()
    _assert_predecessor(competition_database, expected)


def test_item_with_saved_delivery_does_not_compete_again(competition_database):
    connection = competition_database.connection
    _fixed_source(connection, status=2, selection=2)
    _selected_item(connection)
    connection.commit()
    result = outputs.OutputsRepository().grant_file(_candidate(_Seedling(91, 191, 701, 501)),
                                                   new_operation_key(), competition_database)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.delivery_id is not None
    _assert_predecessor(competition_database, False)


def test_saved_no_outputs_failure_does_not_expand(competition_database):
    connection = competition_database.connection
    _fixed_source(connection, status=2, selection=2)
    connection.execute("UPDATE obtain_source_selections SET error_code=1,error_details_json='{}' WHERE id=191")
    connection.commit()
    _assert_predecessor(competition_database, False)


def test_fixed_empty_source_has_no_candidate(competition_database):
    connection = competition_database.connection
    _seed_plan(connection, 3)
    connection.execute("UPDATE actions SET source_resolution_state=2,resolved_source_plan_id=3,"
        " input_fields_json=?,execution_spec_json='{\"selection_mode\":1}' WHERE id=91",
        (json.dumps({"params": {"source": {"plan_instance_id": "3"}}}),))
    connection.commit()
    _assert_predecessor(competition_database, False)


@pytest.mark.parametrize("target,status,restriction,expected", [
    (None, None, None, False), (999, 1, 1, False), (701, 1, 1, True),
    (701, 5, 1, False), (701, 6, 1, False), (701, 6, 3, False),
])
def test_fixed_cleanup_uses_saved_scope_and_item_result(
    competition_database, target, status, restriction, expected,
):
    connection = competition_database.connection
    connection.execute("UPDATE actions SET type=5,target_selection_state=2,execution_spec_json='{}',"
        " source_resolution_state=?,resolved_source_plan_id=?,input_fields_json=? WHERE id=91",
        (None if status == 5 else 2, None if status == 5 else 2,
         json.dumps({"params": {"output_ids": ["701"]} if status == 5
                     else {"source": {"plan_instance_id": "2"}}})))
    if status != 5:
        connection.executemany("INSERT INTO action_dependencies (id,action_id,depends_on_action_id)"
            " VALUES (?,91,?)", ((191, 11), (192, 12)))
    if target == 999:
        _seed_device_file(connection, 599, 11)
        _seed_output(connection, 999, 11, 599)
    if target is not None:
        connection.execute(
            "INSERT INTO cleanup_items (id,action_id,requested_output_id,output_id,status,restriction_state,"
            " final_event_id,error_code,error_details_json) VALUES (191,91,?,?,?,?,?,?,?)",
            (target, target if status != 5 else None, status, restriction,
             1 if status in (5, 6) else None, 1 if status == 5 else None,
             json.dumps({"requested_output_id": "701"}) if status == 5 else None))
    connection.commit()
    _assert_predecessor(competition_database, expected)
