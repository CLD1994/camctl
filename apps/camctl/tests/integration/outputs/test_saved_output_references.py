"""读取和推进取回项时，所有已保存产物引用仍须指向永久记录。"""

from dataclasses import replace
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence.repositories.validation import BusinessStorage
from camctl.outputs.qualification import QualificationOutcome
from camctl.outputs.sources import SelectionMode
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.transactions import commit_operation, event_envelope, row_facts, update_change

from .test_selection_reuse import _save, selection_database, family_database
from .test_read_associations import _command as _read_command
from .test_selection_authority import _NOW
from .test_sources import _seed_output
from ..support.output_source_cases import QueryProbe, reads_table
from ..support.output_planning import capture_operation, EventSequence, event_case, validate_static_event


@pytest.mark.parametrize("entry", ["read", "new_key", "original_key"])
@pytest.mark.parametrize("missing", ["original", "preview", "mismatch"])
def test_saved_references_remain_required_at_every_read_entry(selection_database, entry, missing):
    owned = selection_database
    command, key = (_save(owned, SelectionMode.EXPLICIT_IDS, (704,)) if missing == "mismatch"
                    else _save(owned, SelectionMode.PREVIEW))
    identity = {"original": 701, "preview": 702, "mismatch": 704}[missing]
    owned.connection.execute("DELETE FROM outputs WHERE id=?", (identity,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    if entry == "read":
        with pytest.raises(ConsistencyError):
            outputs.load_selection(owned.connection, 61)
    else:
        result = outputs.OutputsRepository().fix_selection(command,
            key if entry == "original_key" else new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK, result
        assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("entry", ["read", "new_key"])
@pytest.mark.parametrize("registered_later", [False, True])
def test_request_not_found_does_not_imply_a_saved_output(selection_database, entry, registered_later):
    owned = selection_database
    command, _ = _save(owned, SelectionMode.EXPLICIT_IDS, (999,))
    if registered_later:
        _seed_output(owned.connection, 999, 11, 1)
        owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    if entry == "read":
        snapshot = outputs.load_selection(owned.connection, 61)
    else:
        result = outputs.OutputsRepository().fix_selection(command, new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        snapshot = result.value.snapshot
    item, = snapshot.items
    assert (item.requested_output_id, item.output_id, item.error_code) == (999, None, 1)
    assert tuple(owned.connection.iterdump()) == before


def _candidate(source="host"):
    if source == "device":
        return replace(_read_command(), action_id=30, item_id=1, output_id=702, source_device_file_id=502)
    return replace(_read_command(), action_id=30, item_id=1, output_id=703,
                   source_device_file_id=None, source_intermediate_file_id=801, config=None)


@pytest.mark.parametrize("missing", [701, 702])
@pytest.mark.parametrize("entry", ["first", "canceled", "original_key", "new_reuse"])
@pytest.mark.parametrize("source", ["device", "host"])
def test_grant_checks_saved_comparison_references_before_other_exits(selection_database, missing, entry, source):
    owned = selection_database
    if source == "device":
        owned.connection.execute("UPDATE intermediate_files SET size_bytes=200 WHERE id=801")
        owned.connection.commit()
    _save(owned, SelectionMode.PREVIEW)
    candidate, key = _candidate(source), new_operation_key()
    repository = outputs.OutputsRepository()
    if entry in ("original_key", "new_reuse"):
        first = repository.grant_file(candidate, key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        assert first.value.outcome is QualificationOutcome.GRANTED
    if entry == "canceled":
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=30")
    owned.connection.execute("DELETE FROM outputs WHERE id=?", (missing,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = repository.grant_file(candidate, new_operation_key() if entry == "new_reuse" else key, owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("missing", [701, 702])
@pytest.mark.parametrize("future", [False, True])
def test_member_rejection_requires_current_comparison_references(selection_database, missing, future):
    owned = selection_database
    _save(owned, SelectionMode.PREVIEW)
    ids = {"plans": (1,), "actions": (11, 30), "action_dependencies": (41,),
           "obtain_source_selections": (61,), "obtain_items": (1,), "outputs": (701, 702, 703)}
    state = {table: {identity: row_facts(owned.connection, table, identity) for identity in identities}
             for table, identities in ids.items()}
    item = state["obtain_items"][1]
    after = {"status": 4, "error_code": 3,
             "error_details_json": {"output_id": "703", "availability": "missing"}}
    event = event_envelope(3, 3, 21, 2, (update_change("obtain_items", 1,
        {name: item[name] for name in after}, after),), _NOW)
    unavailable = state["outputs"].pop(missing)
    context = EventContext(TransactionRange(3, 3, 3), {("obtain_items", 1): ("action", 30)}, state,
        transaction_rows={**state, "outputs": {**state["outputs"], missing: unavailable}} if future else None)
    with pytest.raises(EventValidationError):
        BusinessStorage().validate_event(event, context)


@pytest.mark.parametrize("state", ["cleaned", "unknown", "new_size"])
def test_saved_comparison_is_not_recomputed_from_current_files(selection_database, state):
    owned = selection_database
    _save(owned, SelectionMode.PREVIEW)
    if state == "new_size":
        owned.connection.execute("UPDATE intermediate_files SET size_bytes=200 WHERE id=801")
    else:
        owned.connection.execute("UPDATE outputs SET availability=?, cleanup_status=?, error_json=? WHERE id=702",
            (3, 4, None) if state == "cleaned" else (5, 1, '{}'))
    owned.connection.commit()
    saved = outputs.load_selection(owned.connection, 61)
    first = saved.items[0]
    assert (first.output_id, first.original_output_id, first.preview_output_id,
            first.preview_size, first.repaired_size) == (703, 701, 702, 100, 80)


@pytest.mark.parametrize("missing", [701, 702])
def test_formal_grant_cannot_consume_incomplete_current_references(selection_database, missing, monkeypatch):
    from copy import deepcopy
    owned=selection_database
    _save(owned,SelectionMode.PREVIEW)
    proposal=capture_operation(owned,lambda copy: outputs.OutputsRepository().grant_file(
        _candidate(),new_operation_key(),copy))
    event,context=event_case(proposal,21)
    validate_static_event(event,context)
    current=deepcopy(proposal.state_rows)
    current['outputs'].pop(missing)
    incomplete=replace(proposal,state_rows=current)
    before=tuple(owned.connection.iterdump())
    key=new_operation_key()
    receipt=commit_operation(EventSequence(proposal.events,incomplete),key,owned)
    assert receipt.kind=='rolled_back',receipt.error
    assert isinstance(receipt.error,EventValidationError),receipt.error
    assert tuple(owned.connection.iterdump())==before


@pytest.mark.parametrize("entry", ["read", "new_key"])
@pytest.mark.parametrize("failure", ["execute", "fetch"])
def test_current_reference_lookup_preserves_error_and_closes_cursor(selection_database, entry, failure):
    owned = selection_database
    command, _ = _save(owned, SelectionMode.PREVIEW)
    probe = QueryProbe(owned.connection,lambda reads,columns,args: ('outputs','id') in reads
        and all(column in ('','id') for table,column in reads if table=='outputs'),failure)
    before = tuple(owned.connection.iterdump())
    if entry == "read":
        with pytest.raises(sqlite3.OperationalError) as raised:
            outputs.load_selection(probe, 61)
        assert raised.value is probe.error
    else:
        result = outputs.OutputsRepository().fix_selection(command, new_operation_key(), replace(owned, connection=probe))
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert result.error is probe.error
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("source", ["device", "host"])
def test_grant_reference_query_error_does_not_create_preparation(selection_database, source):
    owned = selection_database
    if source == "device":
        owned.connection.execute("UPDATE intermediate_files SET size_bytes=200 WHERE id=801")
        owned.connection.commit()
    _save(owned, SelectionMode.PREVIEW)
    error = sqlite3.OperationalError("原片引用查询失败")

    probe=QueryProbe(owned.connection,lambda reads,columns,args:
        reads_table(reads,'outputs') and args==(701,), 'execute',error=error)
    before=tuple(owned.connection.iterdump())
    result=outputs.OutputsRepository().grant_file(_candidate(source),new_operation_key(),
        replace(owned,connection=probe))
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert result.error is error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("fail_second_batch", [False, True])
def test_current_items_are_read_in_bounded_batches_without_partial_result(selection_database, fail_second_batch):
    owned = selection_database
    _save(owned, SelectionMode.EXPLICIT_IDS, tuple(range(1000, 1130)))
    sizes, cursors = [], []
    error = sqlite3.OperationalError("第二批明细读取失败")

    def fetchmany(cursor,size):
        sizes.append(size)
        if fail_second_batch and len(sizes)==2:
            raise error
        return cursor.fetchmany(size)
    probe=QueryProbe(owned.connection,lambda reads,columns,args:
        reads_table(reads,'obtain_items') and 'requested_output_id' in columns,
        fetchmany=fetchmany)
    cursors=probe.cursors
    before = tuple(owned.connection.iterdump())
    if fail_second_batch:
        with pytest.raises(sqlite3.OperationalError) as raised:
            outputs.load_selection(probe, 61)
        assert raised.value is error
        assert len(sizes) == 2
    else:
        snapshot = outputs.load_selection(probe, 61)
        assert tuple(item.requested_output_id for item in snapshot.items) == tuple(range(1000, 1130))
    assert all(0 < size <= 128 for size in sizes)
    for cursor in cursors:
        cursor.close.assert_called_once_with()
    assert tuple(owned.connection.iterdump()) == before
