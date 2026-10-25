"""完整核实后的结果决定按同一事务及集合身份复用。"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.segment_storage import (
    ResultCheckConclusionSave, ResultCheckEndReason, SegmentResultRef,
)
from camctl.contracts.values import ConsistencyError
from camctl.history import decoding
from camctl.history.events import EventEnvelope
from camctl.persistence.read_reuse import clear_read_reuse, read_reuse_scope
from camctl.persistence.repositories import segment_check_conclusions as storage


@pytest.fixture
def verified(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.in_transaction = True
    connection.total_changes = 0
    request = ResultCheckConclusionSave(SegmentResultRef(7, 1, 1),
        ResultCheckEndReason.NO_RESULT_WORK, 18, None, None, None, None, 100)
    evidence = {"result_check_decision": {"original": "request"}}
    event = EventEnvelope(19, 2, 1, 1, 100, 1, None, 1, evidence, ())
    state = SimpleNamespace(connection=connection, found=(11, 19),
        result={"id": 11, "set_id": 1, "failure": {"members": [1]}},
        request=request, evidence=evidence, key="a" * 32)

    def one(db, sql, parameters=()):
        if "FROM segment_check_conclusions" in sql:
            return state.found
        if "FROM history_events" in sql:
            return ("original event",)
        if "FROM history_transactions" in sql:
            return (state.key,)
        raise AssertionError(sql)

    def patch(module, name, **options):
        port = create_autospec(getattr(module, name), **options)
        monkeypatch.setattr(module, name, port)
        return port

    state.one = patch(storage, "_one", side_effect=one)
    state.decode = patch(decoding, "decode_event_row", return_value=event)
    state.build_request = patch(storage, "_request", return_value=request)
    state.verify = patch(storage, "verify_result_check_conclusion", return_value=None)
    state.facts = patch(storage, "row_facts", side_effect=lambda db, table, identity: state.result)
    return state


def test_scoped_identity_reuses_only_after_complete_verification(verified):
    db = verified.connection
    with read_reuse_scope(db):
        first = storage.read_verified_conclusion(db, {"id": 1, "state": "first"})
        second = storage.read_verified_conclusion(db, {"id": 1, "state": "later"})
    assert first == second == verified.result
    assert verified.one.call_count == 3
    verified.decode.assert_called_once_with(("original event",))
    verified.build_request.assert_called_once_with(verified.evidence["result_check_decision"])
    verified.verify.assert_called_once()
    request, key, owned = verified.verify.call_args.args
    assert request is verified.request and key == verified.key and owned.connection is db
    verified.facts.assert_called_once_with(db, "segment_check_conclusions", 11)
    db.execute.assert_not_called()


def test_without_scope_repeats_full_verification(verified):
    db = verified.connection
    assert storage.read_verified_conclusion(db, {"id": 1}) == verified.result
    assert storage.read_verified_conclusion(db, {"id": 1}) == verified.result
    assert verified.one.call_count == 6
    assert verified.verify.call_count == 2


@pytest.mark.parametrize("identity", [2, True, "1"])
def test_different_identity_or_type_does_not_share_conclusion(verified, identity):
    db = verified.connection
    with read_reuse_scope(db):
        storage.read_verified_conclusion(db, {"id": 1})
        assert storage.read_verified_conclusion(db, {"id": identity}) == verified.result
        assert storage.read_verified_conclusion(db, {"id": identity}) == verified.result
    assert verified.one.call_count == 6
    assert verified.verify.call_count == 2


def test_missing_conclusion_is_not_cached_as_a_result(verified):
    db = verified.connection
    verified.found = None
    with read_reuse_scope(db):
        with pytest.raises(ConsistencyError, match="缺少"):
            storage.read_verified_conclusion(db, {"id": 1})
        verified.verify.assert_not_called()
        verified.found = (11, 19)
        assert storage.read_verified_conclusion(db, {"id": 1}) == verified.result
    assert verified.one.call_count == 4
    verified.verify.assert_called_once()


@pytest.mark.parametrize("port,error", [
    ("decode", ConsistencyError("正文无效")),
    ("verify", ConsistencyError("原核验失败")),
    ("facts", sqlite3.OperationalError("事实读取失败")),
])
def test_failed_verification_keeps_original_error_and_retries(verified, port, error):
    db = verified.connection
    failing = getattr(verified, port)
    original = failing.side_effect
    failing.side_effect = error
    with read_reuse_scope(db):
        with pytest.raises(type(error)) as caught:
            storage.read_verified_conclusion(db, {"id": 1})
        assert caught.value is error
        failing.side_effect = original
        assert storage.read_verified_conclusion(db, {"id": 1}) == verified.result
        assert storage.read_verified_conclusion(db, {"id": 1}) == verified.result
    assert failing.call_count == 2


@pytest.mark.parametrize("reset", ["write", "clear", "new_scope"])
def test_new_generation_or_phase_requires_complete_verification(verified, reset):
    db = verified.connection
    with read_reuse_scope(db):
        storage.read_verified_conclusion(db, {"id": 1})
        verified.result = {"id": 11, "set_id": 1, "failure": {"members": [2]}}
        if reset == "write":
            db.total_changes += 1
        elif reset == "clear":
            clear_read_reuse(db)
        if reset != "new_scope":
            assert storage.read_verified_conclusion(db, {"id": 1}) == verified.result
    if reset == "new_scope":
        with read_reuse_scope(db):
            assert storage.read_verified_conclusion(db, {"id": 1}) == verified.result
    assert verified.one.call_count == 6
    assert verified.verify.call_count == 2


def test_mutable_conclusion_results_are_independent(verified):
    db = verified.connection
    with read_reuse_scope(db):
        first = storage.read_verified_conclusion(db, {"id": 1})
        first["failure"]["members"].append(2)
        second = storage.read_verified_conclusion(db, {"id": 1})
        assert second["failure"]["members"] == [1]
        second["failure"]["members"].append(3)
        third = storage.read_verified_conclusion(db, {"id": 1})
        assert third["failure"]["members"] == [1]
    verified.verify.assert_called_once()
