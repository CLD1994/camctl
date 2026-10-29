"""公开目录读取与H1静态当前事实共同核对来源关系。"""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import sqlite3

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError
from camctl.persistence.repositories import outputs
from camctl.persistence.transaction import row_facts
from camctl.persistence.models import DbOutcomeKind
from camctl.outputs.sources import SelectionMode

from ..support.output_planning import validate_static_event, apply_values
from ..support.output_storage_cases import capture_scoped_operation, observed_rows
from .test_output_family import family_database
from .test_catalog_integrity import CatalogConnection, selection_database
from .test_qualification import _seed_processing
from .test_selection_authority import _request, _snapshot, _NOW


def _read(owned, mode=SelectionMode.DEFAULT, requested=()):
    """公开仓储实际生成事件，正式提案范围作为独立H1输入。"""
    _request(owned.connection, mode, requested)
    facts = outputs.load_selection_facts(
        owned.connection, 11, requested_output_ids=requested
    )
    snapshot = _snapshot(owned.connection, mode, requested)
    proposal = capture_scoped_operation(
        owned,
        outputs,
        lambda target: outputs.OutputsRepository().fix_selection(
            outputs.FixSelection(61, snapshot, _NOW), new_operation_key(), target
        ),
    )
    return facts, proposal


def _validate(proposal):
    return validate_static_event(proposal.events[0], proposal)


def test_catalog_read_keeps_raw_rows_and_complete_scopes(selection_database):
    facts, context = _read(selection_database)
    assert tuple(member.output_id for member in facts.outputs) == (701, 702, 703, 705)
    assert set(observed_rows(context, "outputs", "source_action_id", 11)) == {
        701,
        702,
        703,
        705,
    }
    assert observed_rows(context, "output_origins", "output_id", 701) == {}
    assert {
        row["output_id"]
        for row in observed_rows(
            context, "output_origins", "original_output_id", 701
        ).values()
    } == {702, 703}
    assert context.state_rows["device_files"][502]["original_device_file_id"] == 501
    assert context.state_rows["intermediate_files"][801]["size_bytes"] == 80
    assert 504 not in context.state_rows["device_files"]
    assert 704 not in context.state_rows["outputs"]
    assert _validate(context).branch_name == "OBTAIN"


def test_current_catalog_matches_sql_catalog_without_sql_access(selection_database):
    facts, context = _read(selection_database)
    # H1只接收已保存事件和显式事实；其中没有数据库端口。
    assert _validate(context).branch_name == "OBTAIN"
    assert tuple(item.output_id for item in facts.outputs) == (701, 702, 703, 705)
    assert tuple(
        row.after.values["output_id"]
        for row in context.events[0].rows
        if row.table == "obtain_items"
    ) == (703, 705)


@pytest.mark.parametrize(
    "table,column,identity",
    [
        ("outputs", "source_action_id", 11),
        ("output_origins", "output_id", 701),
        ("output_origins", "original_output_id", 701),
    ],
)
def test_current_catalog_requires_complete_relationship_ranges(
    selection_database, table, column, identity
):
    _, context = _read(selection_database)
    ranges = dict(context.read_coverage.ranges)
    ranges[table, column] = ranges[table, column] - {identity}
    with pytest.raises(EventValidationError):
        _validate(replace(context, read_coverage=ReadCoverage(ranges)))


@pytest.mark.parametrize(
    "table,identity,column,value",
    [
        ("device_files", 502, "original_device_file_id", 505),
        ("device_files", 501, "role", 1),
        ("device_files", 501, "completion_state", 1),
        ("intermediate_files", 801, "retention_state", 1),
        ("intermediate_files", 801, "owner_action_id", 12),
    ],
)
def test_current_catalog_rechecks_file_relationships(
    selection_database, table, identity, column, value
):
    _, context = _read(selection_database)
    context.state_rows[table][identity][column] = value
    with pytest.raises(EventValidationError):
        _validate(context)


def test_current_catalog_uses_current_sizes_and_not_future_proposal(selection_database):
    selection_database.connection.execute(
        "UPDATE intermediate_files SET size_bytes=120 WHERE id=801"
    )
    selection_database.connection.commit()
    facts, context = _read(selection_database, SelectionMode.PREVIEW)
    assert {member.output_id: member.size_bytes for member in facts.outputs} == {
        701: 4096,
        702: 100,
        703: 120,
        705: 4096,
    }
    future = deepcopy(context.state_rows)
    apply_values(future, context.events[0])
    future["intermediate_files"][801]["size_bytes"] = 99
    assert _validate(replace(context, transaction_rows=future)).branch_name == "OBTAIN"
    assert tuple(
        row.after.values["output_id"]
        for row in context.events[0].rows
        if row.table == "obtain_items"
    ) == (702, None)


def test_reliable_empty_catalog_can_be_reinterpreted(selection_database):
    connection = selection_database.connection
    connection.execute("DELETE FROM output_origins")
    connection.execute("DELETE FROM outputs WHERE source_action_id=11")
    connection.commit()
    facts, context = _read(selection_database)
    assert facts.outputs == ()
    assert observed_rows(context, "outputs", "source_action_id", 11) == {}
    assert _validate(context).branch_name == "OBTAIN"


@pytest.mark.parametrize("fault", ["execute", "fetch", "decode"])
def test_failed_catalog_does_not_declare_complete_source(selection_database, fault):
    connection = selection_database.connection
    if fault == "decode":
        connection.execute(
            'UPDATE outputs SET media_json=\'{"x":1,"x":2}\' WHERE id=705'
        )
        connection.commit()
    before = tuple(connection.iterdump())
    with pytest.raises(
        ConsistencyError if fault == "decode" else sqlite3.OperationalError
    ):
        outputs.load_selection_facts(CatalogConnection(connection, fault), 11)
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("present", [False, True])
def test_processing_query_preserves_identity_and_reliable_absence(
    selection_database, present
):
    connection = selection_database.connection
    if present:
        _seed_processing(connection, 51, 11, 501)
        connection.commit()
    facts = outputs.load_selection_facts(connection, 11)
    assert facts.source_completed
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.history.reads import ProductReadScope

    connection.execute("BEGIN")
    try:
        records = {
            identity: row
            for page in SqlEventReads(
                connection, ProductReadScope(30, 701, _NOW, 1)
            ).iter_members("recording_processing", "action_id", 11)
            for identity, row in page
        }
        if present:
            record = records[51]
            assert (
                record["id"],
                record["action_id"],
                record["check_state"],
                record["repair_state"],
                record["discard_state"],
            ) == (51, 11, 1, 1, 1)
        else:
            assert records == {}
    finally:
        connection.rollback()


@pytest.mark.parametrize("fault", ["execute", "fetch"])
def test_failed_processing_query_does_not_declare_absence(selection_database, fault):
    connection = selection_database.connection
    before = tuple(connection.iterdump())
    with pytest.raises(sqlite3.OperationalError):
        outputs.load_selection_facts(
            CatalogConnection(connection, fault, "processing"), 11
        )
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("embedded", [705, True])
def test_current_catalog_rejects_conflicting_embedded_identity(
    selection_database, embedded
):
    _, context = _read(selection_database)
    context.state_rows["outputs"][701]["id"] = embedded
    with pytest.raises(EventValidationError):
        _validate(context)


@pytest.mark.parametrize("embedded", [701, Decimal("701.0")])
def test_current_catalog_preserves_equivalent_embedded_identity(
    selection_database, embedded
):
    facts, context = _read(selection_database)
    context.state_rows["outputs"][701]["id"] = embedded
    assert _validate(context).branch_name == "OBTAIN"
    assert tuple(member.output_id for member in facts.outputs) == (701, 702, 703, 705)
    assert context.state_rows["outputs"][701]["id"] is embedded


@pytest.mark.parametrize("lookup_first", [False, True])
def test_existence_lookup_and_catalog_keep_complete_output_fields(
    selection_database, lookup_first
):
    connection = selection_database.connection
    if not lookup_first:
        outputs.load_selection_facts(connection, 12)
    facts = outputs.load_selection_facts(
        connection, 11, requested_output_ids=(704, 999)
    )
    if lookup_first:
        outputs.load_selection_facts(connection, 12)
    assert facts.checked_output_sources == {704: 12, 999: None}
    foreign = outputs.load_selection_facts(connection, 12)
    (current,) = foreign.outputs
    assert (
        current.output_id,
        current.size_bytes,
        row_facts(connection, "outputs", 704)["source_action_id"],
    ) == (704, 4096, 12)


def _partial(connection, entry):
    if entry == "processing":
        return outputs.load_selection_facts(connection, 11).source_completed
    if entry == "origin":
        return outputs.load_output_family(connection, 702)
    if entry == "related":
        return outputs.load_output_family(connection, 701)
    return outputs.load_selection_facts(
        connection, 12, requested_output_ids=(701,)
    ).checked_output_sources


@pytest.mark.parametrize(
    "entry,table,identity,column,expected",
    [
        ("processing", "recording_processing", 51, "source_device_file_id", 501),
        ("origin", "output_origins", 1, "original_output_id", 701),
        ("related", "output_origins", 1, "original_output_id", 701),
        ("existence", "outputs", 701, "device_file_id", 501),
    ],
)
@pytest.mark.parametrize("partial_first", [False, True])
def test_partial_and_full_reads_preserve_fields_in_both_orders(
    selection_database,
    entry,
    table,
    identity,
    column,
    expected,
    partial_first,
):
    connection = selection_database.connection
    _seed_processing(connection, 51, 11, 501)
    connection.commit()
    before = tuple(connection.iterdump())
    if partial_first:
        _partial(connection, entry)
    full = row_facts(connection, table, identity)
    first = _partial(connection, entry)
    assert _partial(connection, entry) == first
    assert row_facts(connection, table, identity) == full
    assert full[column] == expected
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize(
    "mode", [SelectionMode.DEFAULT, SelectionMode.PREVIEW, SelectionMode.EXPLICIT_IDS]
)
def test_first_fix_supplies_complete_current_facts_to_registered_guard(
    selection_database, mode
):
    requested = (703, 704, 999) if mode is SelectionMode.EXPLICIT_IDS else ()
    facts, context = _read(selection_database, mode, requested)
    assert observed_rows(context, "obtain_items", "selection_id", 61) == {}
    assert observed_rows(context, "recording_processing", "action_id", 11) == {}
    if requested:
        assert (
            observed_rows(context, "outputs", "id", 704)[704]["source_action_id"] == 12
        )
        assert observed_rows(context, "outputs", "id", 999) == {}
    assert tuple(member.output_id for member in facts.outputs) == (701, 702, 703, 705)
    assert _validate(context).branch_name == "OBTAIN"
    result = outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(
            61, _snapshot(selection_database.connection, mode, requested), _NOW
        ),
        new_operation_key(),
        selection_database,
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
