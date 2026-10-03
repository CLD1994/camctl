"""真实业务事件的先后顺序决定首次授予所见的产物和竞争者。"""

from dataclasses import replace
import json
import sqlite3

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history import validators
from camctl.history.events import business_columns
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.transaction import (
    TransactionScope, commit_operation, event_envelope, row_change, row_facts, update_change,
)

from .test_product_competition import competition_database
from .test_qualification import qualification_environment, _NOW, _Seedling, _candidate, _seed_output
from .test_selection_event_sequence import _Sequence
from ..operations.test_result_reuse import _FaultConnection


def _repair_file(connection, identity):
    connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id, purpose, relative_path, retention_state,"
        " cleanup_state, size_bytes, created_event_id, last_event_id, change_count)"
        " VALUES (?,11,4,?,3,1,4096,1,1,1)", (identity, f"derived/{identity}.mp4"))


def _grant_plan(connection, command):
    connection.execute("BEGIN")
    try:
        plan = outputs._GrantFileCommand(command, new_operation_key()).plan(TransactionScope(connection, 1, 1))
        assert len(plan.events) == 5, plan.result
        return plan
    finally:
        connection.rollback()


def _scenario(owned, case):
    connection = owned.connection
    command = _candidate(_Seedling(31, 101, 701, 501))
    if case == "host_missing":
        _repair_file(connection, 801)
        connection.execute("UPDATE outputs SET kind=2, device_file_id=NULL, intermediate_file_id=801 WHERE id=701")
        _seed_output(connection, 703, 11, 501)
        connection.execute("INSERT INTO output_origins (output_id,original_output_id) VALUES (701,703)")
        connection.execute("UPDATE obtain_items SET basis=2, original_output_id=703 WHERE id=101")
        command = replace(command, source_device_file_id=None, source_intermediate_file_id=801, config=None)
        connection.commit()
        plan = _grant_plan(connection, command)
        row = update_change("outputs", 701, {"availability": 1, "error_json": None},
                            {"availability": 4, "error_json": {"reason": "source_missing"}})
        change = event_envelope(2, 2, 20, 4, (row,), _NOW)
        plan.owners["outputs", 701] = ("output", 701)
    elif case == "source_finished":
        connection.execute("UPDATE actions SET status=2 WHERE id=12")
        connection.execute("UPDATE actions SET type=5, target_selection_state=1, execution_spec_json='{}',"
            " input_fields_json=? WHERE id=91",
            (json.dumps({"params": {"source": {"plan_instance_id": "2"}}}),))
        connection.commit()
        plan = _grant_plan(connection, command)
        # 完成守卫需要状态之外的取消与执行事实，读取实际完整行。
        plan.state_rows["actions"][12] = row_facts(connection, "actions", 12)
        # 前序完成后会继续判断处理责任，预先可靠查询当前确实没有处理记录。
        reads = outputs._CatalogReads(connection)
        assert reads.processing(12) is None
        ranges = dict(plan.read_coverage.ranges)
        for key, values in reads.read_coverage().ranges.items():
            ranges[key] = ranges.get(key, frozenset()) | values
        plan = replace(plan, read_coverage=ReadCoverage(ranges))
        change = event_envelope(2, 2, 8, 1,
            (update_change("actions", 12, {"status": 2}, {"status": 3}),), _NOW)
        plan.owners["actions", 12] = ("action", 12)
    else:
        assert case == "new_repair"
        _repair_file(connection, 599)
        connection.execute("UPDATE actions SET input_fields_json=?, execution_spec_json=? WHERE id=91",
            (json.dumps({"params": {"source": {"plan_instance_id": "2"}}}),
             json.dumps({"selection_mode": 1})))
        connection.execute(
            "INSERT INTO outputs (id,source_action_id,kind,intermediate_file_id,availability,cleanup_status,"
            " media_json,created_event_id,last_event_id,change_count) VALUES (999,11,2,599,1,1,'{}',1,1,1)")
        connection.execute("INSERT INTO output_origins (id,output_id,original_output_id) VALUES (899,999,701)")
        connection.commit()
        plan = _grant_plan(connection, command)
        created = []
        for table, identity in (("outputs", 999), ("output_origins", 899)):
            values = row_facts(connection, table, identity)
            created.append(row_change(table, identity, {key: values[key] for key in business_columns(table)}))
            plan.owners[table, identity] = ("output", 999)
        change = event_envelope(2, 2, 20, 2, tuple(created), _NOW)
        connection.execute("DELETE FROM output_origins WHERE id=899")
        connection.execute("DELETE FROM outputs WHERE id=999")
        connection.commit()
        del plan.state_rows["outputs"][999]
        del plan.state_rows["output_origins"][899]
        reads = outputs._CatalogReads(connection)
        assert reads.related(701) == ()
        assert reads.origin(999) is None
        ranges = dict(plan.read_coverage.ranges)
        for key, values in reads.read_coverage().ranges.items():
            ranges[key] = ranges.get(key, frozenset()) | values
        plan = replace(plan, read_coverage=ReadCoverage(ranges))
    plan.state_rows["plans"] = {identity: row_facts(connection, "plans", identity) for identity in (1, 2)}
    return plan, change


@pytest.mark.parametrize("case", ["host_missing", "new_repair", "source_finished"])
@pytest.mark.parametrize("change_first", [False, True])
def test_grant_uses_only_preceding_business_events(competition_database, monkeypatch, case, change_first):
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_capture_guards()
    plan, change = _scenario(competition_database, case)
    events = (change, *plan.events) if change_first else (*plan.events, change)
    original = validators.NAMED_GUARDS["read_permission"]
    seen = []

    def inspect(event, current):
        if event.event_type == 21:
            seen.append((current.state_rows["outputs"][701]["availability"],
                current.state_rows["actions"][12]["status"],
                999 in current.state_rows["outputs"],
                any(row["output_id"] == 999 for row in current.state_rows.get("output_origins", {}).values())))
        original(event, current)

    monkeypatch.setitem(validators.NAMED_GUARDS, "read_permission", inspect)
    before = tuple(competition_database.connection.iterdump())
    receipt = commit_operation(_Sequence(events, plan), new_operation_key(), competition_database)
    assert seen == [(4 if case == "host_missing" and change_first else 1,
                     2 if case == "source_finished" and not change_first else 3,
                     case == "new_repair" and change_first, case == "new_repair" and change_first)], receipt.error
    permitted = change_first if case == "new_repair" else not change_first
    if permitted:
        assert receipt.kind == "completed", receipt.error
        assert competition_database.connection.execute(
            "SELECT status,source_dependency,delivery_id FROM obtain_items WHERE id=101").fetchone() == (3, 1, 1)
        assert competition_database.connection.execute("SELECT COUNT(*) FROM file_copies").fetchone() == (1,)
    else:
        assert receipt.kind == "rolled_back", receipt.error
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert tuple(competition_database.connection.iterdump()) == before


@pytest.mark.parametrize("case,change_first,prefix", [
    ("host_missing", False, "UPDATE outputs"),
    ("new_repair", True, "UPDATE obtain_items"),
    ("source_finished", False, "UPDATE actions"),
])
def test_later_write_failure_rolls_back_business_event_and_preparation(
    competition_database, monkeypatch, case, change_first, prefix,
):
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_capture_guards()
    plan, change = _scenario(competition_database, case)
    events = (change, *plan.events) if change_first else (*plan.events, change)
    before = tuple(competition_database.connection.iterdump())
    target = replace(competition_database, connection=_FaultConnection(competition_database.connection, prefix))
    receipt = commit_operation(_Sequence(events, plan), new_operation_key(), target)
    assert receipt.kind == "rolled_back", receipt.error
    assert isinstance(receipt.error, sqlite3.OperationalError), receipt.error
    assert tuple(competition_database.connection.iterdump()) == before
