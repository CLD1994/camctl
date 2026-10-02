"""目录 SQL 读取与当前状态解释使用同一组关系事实。"""

from dataclasses import replace
from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history import validators
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence.repositories import outputs
from camctl.persistence.models import DbOutcomeKind
from camctl.outputs.sources import SelectionMode

from .test_output_family import family_database
from .test_catalog_integrity import CatalogConnection, selection_database
from .test_qualification import _seed_processing
from .test_selection_authority import _request, _snapshot, _NOW


def _read(connection):
    reads = outputs._CatalogReads(connection)
    members = reads.catalog(11)
    context = EventContext(TransactionRange(1, 1, 1), {}, reads.state_rows,
                           read_coverage=reads.read_coverage())
    return members, context


def test_catalog_read_keeps_raw_rows_and_complete_scopes(family_database):
    members, context = _read(family_database.connection)
    assert tuple(member.entry.output_id for member in members) == (701, 702, 703, 705)
    assert set(context.complete_rows("outputs", "source_action_id", 11)) == {701, 702, 703, 705}
    assert context.complete_rows("output_origins", "output_id", 701) == {}
    assert {row["output_id"] for row in context.complete_rows("output_origins", "original_output_id", 701).values()} == {702, 703}
    assert context.state_rows["device_files"][502]["original_device_file_id"] == 501
    assert context.state_rows["intermediate_files"][801]["size_bytes"] == 80
    assert 504 not in context.state_rows["device_files"]
    assert 704 not in context.state_rows["outputs"]


def test_current_catalog_matches_sql_catalog_without_sql_access(family_database):
    members, context = _read(family_database.connection)
    current = outputs._catalog_from_context(context, 11)
    assert tuple(member.entry for member in current) == tuple(member.entry for member in members)


@pytest.mark.parametrize("table,column,identity", [
    ("outputs", "source_action_id", 11),
    ("output_origins", "output_id", 701),
    ("output_origins", "original_output_id", 701),
])
def test_current_catalog_requires_complete_relationship_ranges(family_database, table, column, identity):
    _, context = _read(family_database.connection)
    ranges = dict(context.read_coverage.ranges)
    ranges[table, column] = ranges[table, column] - {identity}
    with pytest.raises(EventValidationError):
        outputs._catalog_from_context(replace(context, read_coverage=ReadCoverage(ranges)), 11)


@pytest.mark.parametrize("table,identity,column,value", [
    ("device_files", 502, "original_device_file_id", 505),
    ("device_files", 501, "role", 1),
    ("device_files", 501, "completion_state", 1),
    ("intermediate_files", 801, "retention_state", 1),
    ("intermediate_files", 801, "owner_action_id", 12),
])
def test_current_catalog_rechecks_file_relationships(family_database, table, identity, column, value):
    _, context = _read(family_database.connection)
    context.state_rows[table][identity][column] = value
    with pytest.raises(ConsistencyError):
        outputs._catalog_from_context(context, 11)


def test_current_catalog_uses_current_sizes_and_not_future_proposal(family_database):
    _, context = _read(family_database.connection)
    future = {"intermediate_files": {801: {"size_bytes": 99}}}
    context.state_rows["intermediate_files"][801]["size_bytes"] = 120
    current = outputs._catalog_from_context(replace(context, transaction_rows=future), 11)
    assert {member.entry.output_id: member.entry.size_bytes for member in current} == {
        701: 4096, 702: 100, 703: 120, 705: 4096}


def test_reliable_empty_catalog_can_be_reinterpreted(family_database):
    connection = family_database.connection
    connection.execute("DELETE FROM output_origins")
    connection.execute("DELETE FROM outputs WHERE source_action_id=11")
    connection.commit()
    members, context = _read(connection)
    assert members == ()
    assert outputs._catalog_from_context(context, 11) == ()


@pytest.mark.parametrize("fault", ["execute", "fetch", "decode"])
def test_failed_catalog_does_not_declare_complete_source(family_database, fault):
    connection = family_database.connection
    if fault == "decode":
        connection.execute('UPDATE outputs SET media_json=\'{"x":1,"x":2}\' WHERE id=705')
        connection.commit()
    reads = outputs._CatalogReads(CatalogConnection(connection, fault))
    with pytest.raises(ConsistencyError if fault == "decode" else sqlite3.OperationalError):
        reads.catalog(11)
    assert not reads.read_coverage().covers("outputs", "source_action_id", 11)


@pytest.mark.parametrize("present", [False, True])
def test_processing_query_preserves_identity_and_reliable_absence(family_database, present):
    connection = family_database.connection
    if present:
        _seed_processing(connection, 51, 11, 501)
        connection.commit()
    reads = outputs._CatalogReads(connection)
    record = reads.processing(11)
    context = EventContext(TransactionRange(1, 1, 1), {}, reads.state_rows,
                           read_coverage=reads.read_coverage())
    current = context.complete_rows("recording_processing", "action_id", 11)
    if present:
        assert current == {51: record}
        assert (record["action_id"], record["check_state"], record["repair_state"], record["discard_state"]) == (11, 1, 1, 1)
    else:
        assert record is None
        assert current == {}


@pytest.mark.parametrize("fault", ["execute", "fetch"])
def test_failed_processing_query_does_not_declare_absence(family_database, fault):
    reads = outputs._CatalogReads(CatalogConnection(family_database.connection, fault, "processing"))
    with pytest.raises(sqlite3.OperationalError):
        reads.processing(11)
    assert not reads.read_coverage().covers("recording_processing", "action_id", 11)


@pytest.mark.parametrize("embedded", [705, True])
def test_current_catalog_rejects_conflicting_embedded_identity(family_database, embedded):
    _, context = _read(family_database.connection)
    context.state_rows["outputs"][701]["id"] = embedded
    with pytest.raises(ConsistencyError):
        outputs._catalog_from_context(context, 11)


@pytest.mark.parametrize("embedded", [701, Decimal("701.0")])
def test_current_catalog_preserves_equivalent_embedded_identity(family_database, embedded):
    _, context = _read(family_database.connection)
    context.state_rows["outputs"][701]["id"] = embedded
    current = outputs._catalog_from_context(context, 11)
    assert tuple(member.entry.output_id for member in current) == (701, 702, 703, 705)
    assert context.state_rows["outputs"][701]["id"] is embedded


@pytest.mark.parametrize("lookup_first", [False, True])
def test_existence_lookup_and_catalog_keep_complete_output_fields(family_database, lookup_first):
    reads = outputs._CatalogReads(family_database.connection)
    local = reads.catalog(11)
    if not lookup_first:
        reads.catalog(12)
    assert reads.checked_sources(tuple(member.entry for member in local), (704, 999)) == {704: 12, 999: None}
    if lookup_first:
        reads.catalog(12)
    context = EventContext(TransactionRange(1, 1, 1), {}, reads.state_rows,
                           read_coverage=reads.read_coverage())
    current, = outputs._catalog_from_context(context, 12)
    assert (current.entry.output_id, current.entry.size_bytes, current.row["source_action_id"]) == (704, 4096, 12)


def _partial(reads, entry):
    if entry == "processing":
        return reads.processing(11)
    if entry == "origin":
        return reads.origin(702)
    if entry == "related":
        return reads.related(701)
    return reads.checked_sources((), (701,))


@pytest.mark.parametrize("entry,table,identity,column,expected", [
    ("processing", "recording_processing", 51, "source_device_file_id", 501),
    ("origin", "output_origins", 1, "original_output_id", 701),
    ("related", "output_origins", 1, "original_output_id", 701),
    ("existence", "outputs", 701, "device_file_id", 501),
])
@pytest.mark.parametrize("partial_first", [False, True])
def test_partial_and_full_reads_preserve_fields_and_full_read_cache(
    family_database, monkeypatch, entry, table, identity, column, expected, partial_first,
):
    connection = family_database.connection
    _seed_processing(connection, 51, 11, 501)
    connection.commit()
    read_row = create_autospec(outputs.row_facts, side_effect=outputs.row_facts)
    monkeypatch.setattr(outputs, "row_facts", read_row)
    reads = outputs._CatalogReads(connection)
    if partial_first:
        _partial(reads, entry)
    reads.required(table, identity)
    _partial(reads, entry)
    _partial(reads, entry)
    assert reads.required(table, identity)[column] == expected
    read_row.assert_called_once_with(connection, table, identity)


@pytest.mark.parametrize("entry,table,identity,column,wrong", [
    ("processing", "recording_processing", 51, "action_id", 12),
    ("origin", "output_origins", 1, "original_output_id", 705),
    ("related", "output_origins", 1, "original_output_id", 705),
    ("existence", "outputs", 701, "source_action_id", 12),
])
def test_overlapping_read_facts_cannot_silently_disagree(
    family_database, monkeypatch, entry, table, identity, column, wrong,
):
    connection = family_database.connection
    _seed_processing(connection, 51, 11, 501)
    connection.commit()
    facts = outputs.row_facts(connection, table, identity)
    facts[column] = wrong
    monkeypatch.setattr(outputs, "row_facts", create_autospec(outputs.row_facts, return_value=facts))
    reads = outputs._CatalogReads(connection)
    reads.required(table, identity)
    with pytest.raises(ConsistencyError):
        _partial(reads, entry)
    assert reads.state_rows[table][identity][column] == wrong


@pytest.mark.parametrize("mode", [SelectionMode.DEFAULT, SelectionMode.PREVIEW, SelectionMode.EXPLICIT_IDS])
def test_first_fix_supplies_complete_current_facts_to_registered_guard(selection_database, monkeypatch, mode):
    owned = selection_database
    requested = (703, 704, 999) if mode is SelectionMode.EXPLICIT_IDS else ()
    _request(owned.connection, mode, requested)
    snapshot = _snapshot(owned.connection, mode, requested)
    original_guard = validators.NAMED_GUARDS["source_selection"]
    observed = []

    def inspect(event, context):
        original_guard(event, context)
        catalog = outputs._catalog_from_context(context, 11)
        assert context.complete_rows("obtain_items", "selection_id", 61) == {}
        assert context.complete_rows("recording_processing", "action_id", 11) == {}
        if requested:
            assert context.complete_rows("outputs", "id", 704)[704]["source_action_id"] == 12
            assert context.complete_rows("outputs", "id", 999) == {}
        observed.append(tuple(member.entry.output_id for member in catalog))

    monkeypatch.setitem(validators.NAMED_GUARDS, "source_selection", inspect)
    result = outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, snapshot, _NOW), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert observed == [(701, 702, 703, 705)]
