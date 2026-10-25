"""显式结论核实每次履行原完整申请，不充当普通消费入口。"""

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


def test_explicit_verification_checks_original_request_every_time(verified):
    db = verified.connection
    first = storage.read_verified_conclusion(db, {"id": 1})
    second = storage.read_verified_conclusion(db, {"id": 1})
    assert first == second == verified.result
    assert verified.one.call_count == 6
    assert verified.decode.call_count == 2
    assert verified.build_request.call_count == 2
    assert verified.verify.call_count == 2
    request, key, owned = verified.verify.call_args.args
    assert request is verified.request and key == verified.key and owned.connection is db
    assert verified.facts.call_count == 2
    db.execute.assert_not_called()


def test_missing_conclusion_does_not_become_success(verified):
    db = verified.connection
    verified.found = None
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
def test_failed_explicit_verification_preserves_error_and_retries(verified, port, error):
    failing = getattr(verified, port)
    original = failing.side_effect
    failing.side_effect = error
    with pytest.raises(type(error)) as caught:
        storage.read_verified_conclusion(verified.connection, {"id": 1})
    assert caught.value is error
    failing.side_effect = original
    assert storage.read_verified_conclusion(verified.connection, {"id": 1}) == verified.result
    assert failing.call_count == 2
