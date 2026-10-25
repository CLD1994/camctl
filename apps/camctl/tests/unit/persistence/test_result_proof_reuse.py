"""一次可靠事务内复用完整证明输入，写入与作用域结束后重新读取。"""

from dataclasses import replace
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.result_pages import ResultPageRef
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.operations.models import AttemptTicket
from camctl.persistence.repositories import result_file_proofs, result_pages, segments


_ENTRIES = ("page", "fixed_file", "file_at", "completion", "strict")


@pytest.fixture(params=_ENTRIES)
def proof_entry(request, monkeypatch):
    """隔离数据库与下一级证明端口，保留被测规范入口的真实计算。"""
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.in_transaction = True
    connection.total_changes = 0
    state = SimpleNamespace(reads=0, failure=None, connection=connection)

    def observed():
        state.reads += 1
        if state.failure is not None:
            raise state.failure

    def patch(module, name, implementation):
        monkeypatch.setattr(module, name, create_autospec(
            getattr(module, name), side_effect=implementation))

    entry = request.param
    if entry == "page":
        def execute(sql, parameters=()):
            assert "FROM history_events WHERE id=?" in sql
            observed()
            if not isinstance(parameters[0], int):
                raise sqlite3.ProgrammingError("数据库不接受该事件身份绑定")
            cursor = create_autospec(sqlite3.Cursor, instance=True)
            cursor.fetchone.return_value = (parameters[0], connection.total_changes)
            return cursor

        connection.execute.side_effect = execute
        patch(result_pages, "decode_event_row", lambda row: SimpleNamespace(
            event_id=row[0], transaction_id=1, generation=row[1]))
        patch(result_pages, "read_transaction_range", lambda db, identity:
              TransactionRange(1, 1, 100))
        patch(result_pages, "_decode", lambda event:
              ({"event_id": event.event_id, "generation": event.generation}, {}))
        patch(result_pages, "_verify_page_relations", lambda *args: None)
        state.call = lambda **changes: result_pages._at_record(
            connection, changes.get("event_id", 10))
        state.variant = {"event_id": 11}
    elif entry == "fixed_file":
        def facts(db, table, identity):
            assert table == "device_files"
            observed()
            if not isinstance(identity, int):
                raise sqlite3.ProgrammingError("数据库不接受该文件身份绑定")
            return {"id": identity, "last_event_id": 30}

        def one(db, sql, parameters=()):
            if "SELECT e.transaction_id" in sql:
                return (parameters[0],)
            if "SELECT MAX(event_id)" in sql:
                return (parameters[-1],)
            if "SELECT t.id,t.last_event_id" in sql:
                return (30, 30)
            raise AssertionError(sql)

        patch(segments, "row_facts", facts)
        patch(segments, "_one", one)
        patch(segments, "read_transaction_range", lambda db, identity:
              TransactionRange(identity, identity, identity))
        patch(segments, "read_row_values_at_boundary", lambda db, **values:
              {"generation": connection.total_changes})
        state.call = lambda **changes: segments._fixed_file(connection,
            changes.get("file_id", 3), changes.get("file_event_id", 10))
        state.variant = {"file_event_id": 11}
    elif entry == "file_at":
        def one(db, sql, parameters):
            assert "SELECT MAX(event_id)" in sql
            observed()
            if not isinstance(parameters[1], int):
                raise sqlite3.ProgrammingError("数据库不接受该文件身份绑定")
            return (parameters[-1],)

        patch(result_file_proofs, "_one", one)
        patch(result_file_proofs, "read_transaction_range", lambda db, identity:
              TransactionRange(identity, identity, identity))
        patch(result_file_proofs, "_fixed_file", lambda db, identity, event_id:
              {"id": identity, "last_event_id": event_id,
               "generation": connection.total_changes})
        state.call = lambda **changes: result_file_proofs._file_at(connection,
            changes.get("file_id", 3), changes.get("boundary", HistoryBoundary(10, 10)))
        state.variant = {"boundary": HistoryBoundary(11, 11)}
    else:
        role = int(enum_for("device_files.role").PREVIEW)

        def proof(db, value, *, current_boundary=None):
            observed()
            return 7, role, {}

        patch(result_file_proofs, "_proof", proof)
        patch(result_file_proofs, "_file_at", lambda db, identity, boundary:
              {"id": identity, "last_event_id": boundary.last_event_id,
               "source_action_id": 7, "role": role,
               "ownership_evidence_json": {"generation": connection.total_changes},
               "pairing_evidence_json": None})
        patch(result_file_proofs, "_completion_page", lambda db, page, file, boundary:
              page.event_id)
        from camctl.persistence.repositories import result_file_pairing

        patch(result_file_pairing, "verify_pairing_fact", lambda db, identity, file_id,
              boundary, *, current_boundary=None: {
                  "state": int(enum_for("device_file_pairing_facts.state").UNPAIRED)})
        page = ResultPageRef(AttemptTicket(1, "result", "2", "results/2", 4), 1, 10, None)
        state.page = page

        def call(**changes):
            arguments = dict(current_boundary=changes.get("current_boundary"))
            function = result_file_proofs.load_completion_input
            if entry == "strict":
                function = result_file_proofs.load_strict_result_file_input
                arguments["pairing_fact_id"] = changes.get("pairing_fact_id")
            return function(connection, changes.get("page", page),
                changes.get("file_id", 3),
                changes.get("boundary", HistoryBoundary(20, 20)), **arguments)

        state.call = call
        state.variant = {"current_boundary": HistoryBoundary(30, 30)}
    state.entry = entry
    return state


def _scope(connection):
    from camctl.persistence.read_reuse import read_reuse_scope

    return read_reuse_scope(connection)


def test_repeated_proof_input_loads_once_within_transaction(proof_entry):
    with _scope(proof_entry.connection):
        first = proof_entry.call()
        second = proof_entry.call()
    assert second == first
    assert proof_entry.reads == 1


def test_distinct_proof_input_cannot_reuse_previous_result(proof_entry):
    with _scope(proof_entry.connection):
        proof_entry.call()
        proof_entry.call(**proof_entry.variant)
    assert proof_entry.reads == 2


def test_failed_proof_is_retried_and_preserves_original_error(proof_entry):
    error = ConsistencyError("原证明不可解释")
    proof_entry.failure = error
    with _scope(proof_entry.connection):
        with pytest.raises(ConsistencyError) as caught:
            proof_entry.call()
        assert caught.value is error
        proof_entry.failure = None
        first = proof_entry.call()
        assert proof_entry.call() == first
    assert proof_entry.reads == 2


def test_write_rechecks_proof_in_same_transaction(proof_entry):
    with _scope(proof_entry.connection):
        proof_entry.call()
        proof_entry.connection.total_changes += 1
        proof_entry.call()
    assert proof_entry.reads == 2


def test_new_scope_rechecks_proof(proof_entry):
    with _scope(proof_entry.connection):
        proof_entry.call()
    with _scope(proof_entry.connection):
        proof_entry.call()
    assert proof_entry.reads == 2


@pytest.mark.parametrize("proof_entry", ["completion", "strict"], indirect=True)
@pytest.mark.parametrize("field", ["ticket", "page_no", "event_id", "next_cursor",
                                   "file_id", "boundary", "current_boundary"])
def test_file_proof_key_contains_complete_reference(proof_entry, field):
    page = proof_entry.page
    changes = {
        "ticket": {"page": replace(page, ticket=replace(page.ticket, run_id=5))},
        "page_no": {"page": replace(page, page_no=2)},
        "event_id": {"page": replace(page, event_id=11)},
        "next_cursor": {"page": replace(page, next_cursor=DirectoryCursor(
            DeviceBinding("camera", "driver"), ("/DCIM",), 0, None))},
        "file_id": {"file_id": 4},
        "boundary": {"boundary": HistoryBoundary(21, 21)},
        "current_boundary": {"current_boundary": HistoryBoundary(30, 30)},
    }
    with _scope(proof_entry.connection):
        proof_entry.call()
        proof_entry.call(**changes[field])
    assert proof_entry.reads == 2


@pytest.mark.parametrize("proof_entry", ["strict"], indirect=True)
def test_strict_proof_key_contains_requested_pairing_fact(proof_entry):
    with _scope(proof_entry.connection):
        first = proof_entry.call(pairing_fact_id=55)
        second = proof_entry.call(pairing_fact_id=56)
    assert (first.pairing_fact_id, second.pairing_fact_id) == (55, 56)


@pytest.mark.parametrize("proof_entry", ["completion", "strict"], indirect=True)
@pytest.mark.parametrize("field", ["page_type", "ticket_type"])
def test_file_proof_key_preserves_reference_types(proof_entry, field):
    class OtherPage(ResultPageRef):
        pass

    class OtherTicket(AttemptTicket):
        pass

    page = proof_entry.page
    variants = {
        "page_type": OtherPage(page.ticket, page.page_no, page.event_id, page.next_cursor),
        "ticket_type": replace(page, ticket=OtherTicket(1, "result", "2", "results/2", 4)),
    }
    with _scope(proof_entry.connection):
        proof_entry.call()
        proof_entry.call(page=variants[field])
    assert proof_entry.reads == 2


@pytest.mark.parametrize("proof_entry", ["completion", "strict"], indirect=True)
@pytest.mark.parametrize("field", ["page_no", "event_id", "run_id", "attempt_id"])
@pytest.mark.parametrize("invalid", [True, 1.0])
def test_warm_file_proof_still_validates_integer_reference_fields(proof_entry, field, invalid):
    page = replace(proof_entry.page, event_id=1,
                   ticket=replace(proof_entry.page.ticket, run_id=1))
    changed = (replace(page, **{field: invalid}) if field in ("page_no", "event_id")
               else replace(page, ticket=replace(page.ticket, **{field: invalid})))
    with _scope(proof_entry.connection):
        proof_entry.call(page=page)
        with pytest.raises(ValueError):
            proof_entry.call(page=changed)


@pytest.mark.parametrize("proof_entry", ["completion", "strict"], indirect=True)
@pytest.mark.parametrize("invalid", [True, 1.0])
def test_warm_file_proof_still_rejects_noninteger_file_id(proof_entry, invalid):
    with _scope(proof_entry.connection):
        proof_entry.call(file_id=1)
        with pytest.raises(ValueError):
            proof_entry.call(file_id=invalid)


@pytest.mark.parametrize("proof_entry", ["strict"], indirect=True)
@pytest.mark.parametrize("invalid", [True, 1.0])
def test_warm_strict_proof_still_rejects_noninteger_pairing_id(proof_entry, invalid):
    with _scope(proof_entry.connection):
        proof_entry.call(pairing_fact_id=1)
        with pytest.raises(ValueError):
            proof_entry.call(pairing_fact_id=invalid)


@pytest.mark.parametrize("proof_entry", ["completion", "strict"], indirect=True)
@pytest.mark.parametrize("invalid", [[], {}])
def test_unhashable_file_id_keeps_original_value_error(proof_entry, invalid):
    with _scope(proof_entry.connection):
        with pytest.raises(ValueError):
            proof_entry.call(file_id=invalid)


@pytest.mark.parametrize("proof_entry", ["strict"], indirect=True)
@pytest.mark.parametrize("invalid", [[], {}])
def test_unhashable_pairing_id_keeps_original_value_error(proof_entry, invalid):
    with _scope(proof_entry.connection):
        with pytest.raises(ValueError):
            proof_entry.call(pairing_fact_id=invalid)


@pytest.mark.parametrize("proof_entry", ["completion", "strict"], indirect=True)
@pytest.mark.parametrize("argument", ["page", "boundary"])
@pytest.mark.parametrize("invalid", [[], {}])
def test_unhashable_reference_keeps_original_type_error(proof_entry, argument, invalid):
    with _scope(proof_entry.connection):
        with pytest.raises(TypeError):
            proof_entry.call(**{argument: invalid})
    assert proof_entry.reads == 0


@pytest.mark.parametrize("proof_entry", ["page", "fixed_file", "file_at"], indirect=True)
def test_private_read_preserves_unhashable_database_binding_error(proof_entry):
    argument = "event_id" if proof_entry.entry == "page" else "file_id"
    with _scope(proof_entry.connection):
        with pytest.raises(sqlite3.ProgrammingError):
            proof_entry.call(**{argument: []})


@pytest.mark.parametrize("proof_entry", ["file_at"], indirect=True)
def test_private_file_read_keeps_original_invalid_boundary_error(proof_entry):
    with _scope(proof_entry.connection):
        with pytest.raises(AttributeError):
            proof_entry.call(boundary=[])


@pytest.mark.parametrize("proof_entry", ["page", "completion", "strict"], indirect=True)
def test_bounded_page_scope_rechecks_proof_warmed_in_generic_scope(proof_entry):
    from camctl.persistence.page_read_cache import page_read_scope

    error = ConsistencyError("独立页范围必须重新核验")
    with _scope(proof_entry.connection):
        proof_entry.call()
        proof_entry.failure = error
        with page_read_scope(proof_entry.connection, 100):
            with pytest.raises(ConsistencyError) as caught:
                proof_entry.call()
    assert caught.value is error
    assert proof_entry.reads == 2


@pytest.mark.parametrize("proof_entry", ["completion", "strict"], indirect=True)
def test_bounded_page_scope_does_not_cache_current_file_qualification(proof_entry):
    from camctl.persistence.page_read_cache import page_read_scope

    error = ConsistencyError("当前资格必须实际核实")
    with _scope(proof_entry.connection), page_read_scope(proof_entry.connection, 100):
        proof_entry.call()
        proof_entry.failure = error
        with pytest.raises(ConsistencyError) as caught:
            proof_entry.call()
    assert caught.value is error
    assert proof_entry.reads == 2


@pytest.mark.parametrize("proof_entry", ["page"], indirect=True)
def test_generic_scope_does_not_extend_three_page_window(proof_entry):
    from camctl.persistence.page_read_cache import page_read_scope

    with _scope(proof_entry.connection), page_read_scope(proof_entry.connection, 100):
        for identity in (10, 11, 12, 13, 10):
            proof_entry.call(event_id=identity)
    assert proof_entry.reads == 5
