"""逐产物候选的真实读取范围与当前事实边界。"""

from copy import deepcopy
from dataclasses import replace
import json
import sqlite3

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError
from ..support.output_planning import validate_static_event as validate_event
from ..support.output_storage_cases import (
    product_proposal,
    observed_rows,
    observe_product_competition,
    QueryProbe,
)
from ..support.outputs import observed_query
from ..support.output_planning import capture_operation, EventSequence
from camctl.persistence.repositories import outputs
from camctl.persistence.transaction import commit_operation

from .test_qualification import (
    qualification_environment, _NOW, _Seedling, _candidate, _seed_action,
    _seed_device_file, _seed_output, _seed_plan, _seed_selection_and_item,
)


@pytest.fixture
def competition_database(qualification_environment):
    owned = qualification_environment
    connection = owned.connection
    _seed_plan(connection, 1)
    _seed_plan(connection, 2)
    for identity in (11, 12):
        _seed_action(connection, identity, 2, action_type=2, status=3)
        connection.execute("UPDATE actions SET group_name=?, input_fields_json=? WHERE id=?",
            ("wanted" if identity == 11 else "unrelated",
             json.dumps({"params": {"description": "正文" * 10000}}), identity))
    _seed_action(connection, 31, 1, action_type=4)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    _seed_selection_and_item(connection, dependency_id=101, selection_id=101, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701)
    _seed_action(connection, 91, 1, action_type=4, scheduled_at=_NOW - 1_000_000)
    connection.execute("UPDATE actions SET source_resolution_state=1, input_fields_json=?,"
        " execution_spec_json=? WHERE id=91",
        (json.dumps({"params": {"source": {"plan_instance_id": "2"}, "filter": "preview"}}),
         json.dumps({"selection_mode": 2})))
    connection.commit()
    return owned


@pytest.mark.parametrize("reference", ["instance", "name", "group", "plan", "fixed"])
def test_source_resolution_retains_metadata_without_request_bodies(
    competition_database, reference
):
    """来源引用的完整性不应以加载终态动作正文为代价。"""
    connection = competition_database.connection
    source = {
        "instance": {"action_instance_id": "11"},
        "name": {"action_name": "action-11"},
        "group": {"plan_instance_id": "2", "group": "wanted"},
        "plan": {"plan_instance_id": "2"},
        "fixed": {"plan_instance_id": "2"},
    }[reference]
    if reference == "name":
        connection.execute("UPDATE actions SET plan_id=2 WHERE id=91")
    connection.execute(
        "UPDATE actions SET input_fields_json=? WHERE id=91",
        (json.dumps({"params": {"source": source, "filter": "preview"}}),),
    )
    if reference == "fixed":
        connection.execute(
            "UPDATE actions SET source_resolution_state=2, resolved_source_plan_id=2 WHERE id=91"
        )
        connection.execute("INSERT INTO action_dependencies VALUES (191,91,11)")
        connection.execute(
            "INSERT INTO obtain_source_selections (id,dependency_id,status) VALUES (191,191,1)"
        )
    connection.commit()
    queries = []

    class MetadataProbe:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            cursor, columns, reads = observed_query(self.connection, sql, parameters)
            json_fields = {
                column
                for table, column in reads
                if table == "actions" and column.endswith("_json")
            }
            if json_fields and tuple(parameters) != (91,):
                cursor.close()
                raise AssertionError("来源元数据查询读取了动作正文")
            queries.append((columns, reads, parameters))
            return cursor

    before = tuple(connection.iterdump())
    from camctl.outputs.sources import SourceSpec, resolve_source

    lookup = outputs.SqliteSourceLookup(MetadataProbe(connection), 91)
    if reference == "fixed":
        sources = tuple(
            row[0]
            for row in connection.execute(
                "SELECT depends_on_action_id FROM action_dependencies WHERE action_id=91 ORDER BY id"
            )
        )
        assert sources == (11,)
        assert lookup.action_by_id(sources[0]).plan_id == 2
    else:
        spec = SourceSpec(
            **{
                name: int(value) if name.endswith("_id") else value
                for name, value in source.items()
            }
        )
        resolution = resolve_source(spec, lookup)
        assert resolution.member_action_ids == (
            (11, 12) if reference == "plan" else (11,)
        )
    assert tuple(connection.iterdump()) == before
    assert queries
    event, proposal = _proposal(competition_database)
    if reference in ("name", "group", "plan"):
        assert set(observed_rows(proposal, "actions", "plan_id", 2)) == (
            {11, 12, 91} if reference == "name" else {11, 12}
        )
    assert validate_event(event, proposal).branch_name == "GRANT"


def _proposal(owned):
    return product_proposal(owned, _candidate(_Seedling(31, 101, 701, 501)))


def test_formal_grant_accepts_complete_candidate_facts(competition_database):
    event, context = _proposal(competition_database)
    assert validate_event(event, context).branch_name == "GRANT"


@pytest.mark.parametrize("table,column,value", [
    ("actions", "status", 1), ("actions", "status", 2),
    ("actions", "plan_id", 2), ("cleanup_items", "output_id", 701),
    ("action_dependencies", "action_id", 91),
    ("recording_processing", "action_id", 11),
])
def test_formal_grant_requires_complete_ranges(competition_database, table, column, value):
    event, context = _proposal(competition_database)
    ranges = dict(context.read_coverage.ranges)
    ranges[table, column] = ranges[table, column] - {value}
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, read_coverage=ReadCoverage(ranges)))


def test_future_ineligible_candidate_cannot_replace_current_predecessor(competition_database):
    event, context = _proposal(competition_database)
    future = deepcopy(context.state_rows)
    candidate = context.state_rows["actions"][91]
    candidate["input_fields_json"]["params"]["filter"] = "default"
    candidate["execution_spec_json"] = {"selection_mode": 1}
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, transaction_rows=future))


def test_future_predecessor_does_not_block_current_grant(competition_database):
    event, context = _proposal(competition_database)
    future = deepcopy(context.state_rows)
    future["actions"][91]["input_fields_json"]["params"]["filter"] = "default"
    future["actions"][91]["execution_spec_json"] = {"selection_mode": 1}
    assert validate_event(event, replace(context, transaction_rows=future)).branch_name == "GRANT"


@pytest.mark.parametrize("column", ["input_fields_json", "execution_spec_json", "status"])
def test_future_candidate_fields_cannot_fill_missing_current_facts(competition_database, column):
    event, context = _proposal(competition_database)
    future = deepcopy(context.state_rows)
    del context.state_rows["actions"][91][column]
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, transaction_rows=future))


def test_source_metadata_can_be_upgraded_to_full_candidate(competition_database):
    connection = competition_database.connection
    connection.execute(
        "UPDATE actions SET input_fields_json=?, execution_spec_json=? WHERE id=91",
        (
            json.dumps({"params": {"source": {"plan_instance_id": "2"}}}),
            json.dumps({"selection_mode": 1}),
        ),
    )
    connection.commit()
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.history.reads import ProductReadScope

    observed = []

    class MetadataFirst:
        def __init__(self, connection):
            self.connection = connection
            self.read = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            before = self.connection.in_transaction
            cursor = self.connection.execute(sql, parameters)
            if not before and self.connection.in_transaction and not self.read:
                self.read = True
                view = SqlEventReads(
                    self.connection, ProductReadScope(31, 701, _NOW, 128)
                )
                rows = {
                    identity: row
                    for batch in view.iter_members(
                        "actions", "plan_id", 1, columns=view.SOURCE_COLUMNS
                    )
                    for identity, row in batch
                }
                observed.append(rows[91])
            return cursor

    assert (
        observe_product_competition(
            competition_database, occurred_at=_NOW, observer=MetadataFirst
        )
        is True
    )
    assert observed and "input_fields_json" not in observed[0]


@pytest.mark.parametrize(
    "source,table,column,identity",
    [
        ({"plan_instance_id": "3"}, "actions", "plan_id", 3),
        ({"plan_instance_id": "4"}, "plans", "id", 4),
        ({"action_instance_id": "99"}, "actions", "id", 99),
    ],
)
def test_absent_or_empty_source_has_reliable_coverage(
    competition_database, source, table, column, identity
):
    connection = competition_database.connection
    _seed_plan(connection, 3)
    connection.execute(
        "UPDATE actions SET input_fields_json=? WHERE id=91",
        (json.dumps({"params": {"source": source, "filter": "preview"}}),),
    )
    connection.commit()
    event, context = _proposal(competition_database)
    assert observed_rows(context, table, column, identity) == {}
    assert validate_event(event, context).branch_name == "GRANT"
    ranges = dict(context.read_coverage.ranges)
    ranges[table, column] = ranges[table, column] - {identity}
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, read_coverage=ReadCoverage(ranges)))


def test_incomplete_candidate_reads_roll_back_entire_grant(competition_database):
    owned = competition_database
    proposal = capture_operation(
        owned,
        lambda actual: outputs.OutputsRepository().grant_file(
            _candidate(_Seedling(31, 101, 701, 501)), new_operation_key(), actual
        ),
    )
    proposal = replace(proposal, product_read_scope=None, read_coverage=ReadCoverage())
    before = tuple(owned.connection.iterdump())
    result = commit_operation(
        EventSequence(proposal.events, proposal), new_operation_key(), owned
    )
    assert result.kind == "rolled_back"
    assert isinstance(result.error, EventValidationError), result.error
    assert tuple(owned.connection.iterdump()) == before
    assert not owned.connection.in_transaction


class _BatchReads(QueryProbe):
    def __init__(self, connection, scope, fail_at=None):
        def selected(columns, reads, parameters):
            return (
                columns == {"id"}
                and ("actions", "status" if scope == "active" else "plan_id") in reads
            )

        super().__init__(connection, selected, failure=fail_at)


def _seed_many_actions(connection, scope):
    for identity in range(1000, 1131):
        _seed_action(connection, identity, 1 if scope == "active" else 2,
                     action_type=2, status=1 if scope == "active" else 3)
    connection.commit()


@pytest.mark.parametrize("scope", ["active", "plan"])
def test_candidate_metadata_spans_bounded_batches(competition_database, scope):
    owned = competition_database
    _seed_many_actions(owned.connection, scope)
    reads = _BatchReads(owned.connection, scope)
    result = outputs.OutputsRepository().grant_file(_candidate(_Seedling(31, 101, 701, 501)),
        new_operation_key(), replace(owned, connection=reads))
    assert result.kind.value == "completed", result.error
    assert result.value.outcome.value == "granted"
    assert len(reads.sizes) >= 2
    assert all(0 < size <= 128 for size in reads.sizes)
    assert reads.cursors
    for cursor in reads.cursors:
        cursor.close.assert_called_once()


@pytest.mark.parametrize("scope", ["active", "plan"])
def test_predecessor_discovered_after_first_batch_still_blocks(competition_database, scope):
    owned = competition_database
    connection = owned.connection
    _seed_many_actions(connection, scope)
    if scope == "active":
        _seed_action(connection, 2000, 1, action_type=5, scheduled_at=_NOW - 1_000_000)
        connection.execute("UPDATE actions SET target_selection_state=1, input_fields_json=? WHERE id=2000",
            (json.dumps({"params": {"output_ids": ["701"]}}),))
    else:
        # 跨计划组的唯一匹配来源位于第二批，前一批成员均属于其他分组。
        connection.execute("UPDATE actions SET group_name='unrelated' WHERE id=11")
        connection.execute("UPDATE actions SET group_name='wanted' WHERE id=1130")
        connection.execute("UPDATE outputs SET source_action_id=1130 WHERE id=701")
        connection.execute("UPDATE device_files SET source_action_id=1130, observer_action_id=1130 WHERE id=501")
        connection.execute("UPDATE action_dependencies SET depends_on_action_id=1130 WHERE id=101")
        connection.execute("UPDATE actions SET input_fields_json=?, execution_spec_json=? WHERE id=91",
            (json.dumps({"params": {"source": {"plan_instance_id": "2", "group": "wanted"}}}),
             json.dumps({"selection_mode": 1})))
    connection.commit()
    before = tuple(connection.iterdump())
    result = outputs.OutputsRepository().grant_file(_candidate(_Seedling(31, 101, 701, 501)),
        new_operation_key(), owned)
    assert result.kind.value == "completed", result.error
    assert result.value.reason == "business_order"
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("scope", ["active", "plan"])
@pytest.mark.parametrize("failure", ["execute", 1, 2])
def test_candidate_query_failure_rolls_back_without_qualification(competition_database, scope, failure):
    owned = competition_database
    _seed_many_actions(owned.connection, scope)
    before = tuple(owned.connection.iterdump())
    reads = _BatchReads(owned.connection, scope, failure)
    result = outputs.OutputsRepository().grant_file(_candidate(_Seedling(31, 101, 701, 501)),
        new_operation_key(), replace(owned, connection=reads))
    assert result.kind.value == "rolled_back"
    assert isinstance(result.error, sqlite3.OperationalError), result.error
    assert tuple(owned.connection.iterdump()) == before
    assert owned.connection.in_transaction is False
    assert bool(reads.cursors) is (failure != "execute")
    for cursor in reads.cursors:
        cursor.close.assert_called_once()
