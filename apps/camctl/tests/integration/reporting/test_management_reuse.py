"""变化正文省略未变字段时，报告原键仍恢复原事务的管理结果。"""

from contextlib import closing
import json
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.reporting.policy import (
    publish_report, record_report_bytes, record_report_failure, record_report_publish_intent,
)
from camctl.reporting import policy

from .test_publish import connection, _bytes, _freeze_report, _moved, _owned, _publish, _update


ERROR = {"reason": "report file", "context": {"ready": True, "count": 1}}


def _completed(result):
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return result.value


def _call(connection, report_id, phase, key, *, occurred_at=1, contents=None, error=None):
    owned = _owned(connection)
    if phase == "prepare":
        return record_report_bytes(key, owned, report_id, _bytes() if contents is None else contents,
                                   occurred_at=occurred_at)
    if phase == "intent":
        return record_report_publish_intent(key, owned, report_id, occurred_at=occurred_at)
    if phase == "publish":
        return publish_report(key, owned, report_id, _moved(), occurred_at=occurred_at)
    return record_report_failure(key, owned, report_id, ERROR if error is None else error,
                                 occurred_at=occurred_at)


def _original(connection, tmp_path, phase):
    report_id = _freeze_report(tmp_path, connection)
    if phase in ("intent", "publish"):
        _completed(_call(connection, report_id, "prepare", new_operation_key()))
    if phase == "publish":
        _completed(_call(connection, report_id, "intent", new_operation_key()))
    key = new_operation_key()
    value = _completed(_call(connection, report_id, phase, key))
    return report_id, key, value


def _readonly(connection, report_id, phase, key, **inputs):
    before = tuple(connection.iterdump())
    result = _call(connection, report_id, phase, key, **inputs)
    assert tuple(connection.iterdump()) == before
    return result


@pytest.mark.parametrize("later", ["none", "intent", "publish", "failure", "republish"])
def test_rebuilt_prepare_key_recovers_unchanged_bytes(connection, tmp_path, later):
    report_id, _, _ = _original(connection, tmp_path, "prepare")
    _completed(_call(connection, report_id, "failure", new_operation_key()))
    key = new_operation_key()
    first = _completed(_call(connection, report_id, "prepare", key))
    with closing(connection.execute("SELECT body_json FROM history_events ORDER BY id DESC LIMIT 1")) as cursor:
        values = json.loads(cursor.fetchone()[0])["rows"][0]["after"]["values"]
    assert "size_bytes" not in values
    assert "sha256" not in values
    if later == "intent":
        _completed(_call(connection, report_id, "intent", new_operation_key()))
    elif later in ("publish", "republish"):
        _completed(_publish(connection, report_id))
        if later == "republish":
            _completed(_publish(connection, report_id))
    elif later == "failure":
        _completed(_call(connection, report_id, "failure", new_operation_key(), error={"reason": "later"}))
    assert _completed(_readonly(connection, report_id, "prepare", key)) == first == _bytes()


@pytest.mark.parametrize("later", ["none", "prepare", "publish"])
def test_same_error_with_stage_change_is_saved_and_reusable(connection, tmp_path, later):
    report_id, _, _ = _original(connection, tmp_path, "prepare")
    _completed(_call(connection, report_id, "failure", new_operation_key()))
    _completed(_call(connection, report_id, "intent", new_operation_key()))
    key = new_operation_key()
    assert _completed(_call(connection, report_id, "failure", key)) is None
    with closing(connection.execute("SELECT body_json FROM history_events ORDER BY id DESC LIMIT 1")) as cursor:
        values = json.loads(cursor.fetchone()[0])["rows"][0]["after"]["values"]
    assert values == {"status": 5}
    if later in ("prepare", "publish"):
        _completed(_call(connection, report_id, "prepare", new_operation_key()))
        if later == "publish":
            _completed(_publish(connection, report_id))
    assert _completed(_readonly(connection, report_id, "failure", key)) is None


@pytest.mark.parametrize("phase", ["prepare", "intent", "publish", "failure"])
@pytest.mark.parametrize("occurred_at", [2, True, 1.0])
def test_management_original_key_rejects_different_or_invalid_time(connection, tmp_path, phase, occurred_at):
    report_id, key, _ = _original(connection, tmp_path, phase)
    result = _readonly(connection, report_id, phase, key, occurred_at=occurred_at)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError if occurred_at == 2 else ValueError), result.error


@pytest.mark.parametrize("phase", ["prepare", "intent", "publish", "failure"])
def test_original_management_result_survives_later_publish_and_error(connection, tmp_path, phase):
    report_id, key, original = _original(connection, tmp_path, phase)
    if phase == "failure":
        _completed(_call(connection, report_id, "prepare", new_operation_key()))
    if phase == "intent":
        _completed(_call(connection, report_id, "publish", new_operation_key()))
    else:
        _completed(_publish(connection, report_id))
    _completed(_call(connection, report_id, "failure", new_operation_key(), error={"reason": "later error"}))
    _completed(_publish(connection, report_id))
    assert _completed(_readonly(connection, report_id, phase, key)) == original


@pytest.mark.parametrize("mutation", ["missing_bytes", "different_bytes", "bad_body"])
def test_intent_reuse_requires_original_first_byte_fact(connection, tmp_path, mutation):
    report_id, key, _ = _original(connection, tmp_path, "intent")
    with closing(connection.execute("SELECT id, body_json FROM history_events WHERE event_type = 28"
                                    " AND json_extract(body_json, '$.reason') = 2 ORDER BY id LIMIT 1")) as cursor:
        event_id, raw = cursor.fetchone()
    body = json.loads(raw)
    if mutation == "bad_body":
        body = {}
    elif mutation == "different_bytes":
        body["rows"][0]["after"]["values"]["sha256"] = "a" * 64
    else:
        for side in ("before", "after"):
            for field in ("size_bytes", "sha256"):
                del body["rows"][0][side]["values"][field]
    connection.execute("UPDATE history_events SET body_json = ? WHERE id = ?", (json.dumps(body), event_id))
    result = _readonly(connection, report_id, "intent", key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.mark.parametrize("phase", ["prepare", "intent", "publish", "failure"])
@pytest.mark.parametrize("prefix", ["SELECT l.change_count", "SELECT event_id, change_count FROM entity_event_links",
                                  "SELECT link.event_id, txn.operation_key"])
def test_original_management_restore_failure_preserves_exception_and_database(connection, tmp_path, phase, prefix):
    report_id, key, _ = _original(connection, tmp_path, phase)
    _completed(_call(connection, report_id, "failure", new_operation_key(), error={"reason": "later"}))
    problem = sqlite3.OperationalError("原报告恢复读取失败")

    class FailingRead:
        def execute(self, sql, parameters=()):
            if sql.startswith(prefix):
                raise problem
            return connection.execute(sql, parameters)

        @property
        def in_transaction(self):
            return connection.in_transaction

    before = tuple(connection.iterdump())
    result = _call(FailingRead(), report_id, phase, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert result.error is problem
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("phase", ["prepare", "failure"])
@pytest.mark.parametrize("committed", [False, True])
def test_rebuild_or_same_error_unknown_commit_is_resolved_by_original_key(connection, tmp_path, phase, committed):
    report_id, _, _ = _original(connection, tmp_path, "prepare")
    _completed(_call(connection, report_id, "failure", new_operation_key()))
    if phase == "failure":
        _completed(_call(connection, report_id, "intent", new_operation_key()))
    key = new_operation_key()

    class LostReceipt:
        def execute(self, sql, parameters=()):
            if sql == "COMMIT":
                if committed:
                    connection.execute(sql, parameters)
                raise sqlite3.OperationalError("没有取得提交响应")
            return connection.execute(sql, parameters)

    result = _call(LostReceipt(), report_id, phase, key)
    assert result.kind is DbOutcomeKind.UNKNOWN, result.error
    if connection.in_transaction:
        connection.rollback()
    if committed:
        _completed(_publish(connection, report_id))
        retry = _readonly(connection, report_id, phase, key)
    else:
        retry = _call(connection, report_id, phase, key)
    assert _completed(retry) == (_bytes() if phase == "prepare" else None)


def test_original_failure_before_bytes_does_not_adopt_later_first_bytes(connection, tmp_path):
    report_id, key, _ = _original(connection, tmp_path, "failure")
    _completed(_call(connection, report_id, "prepare", new_operation_key()))
    _completed(_publish(connection, report_id))
    assert _completed(_readonly(connection, report_id, "failure", key)) is None


def test_original_intent_survives_report_history_crossing_restore_page(connection, tmp_path):
    report_id, key, original = _original(connection, tmp_path, "intent")
    for index in range(129):
        _completed(_call(connection, report_id, "failure", new_operation_key(), error={"index": index}))
    assert _completed(_readonly(connection, report_id, "intent", key)) == original == _bytes()


@pytest.mark.parametrize("phase,requested", [("prepare", "intent"), ("intent", "publish"),
                                           ("publish", "failure"), ("failure", "prepare")])
def test_management_wrong_phase_is_rejected_before_current_management(connection, tmp_path, monkeypatch, phase, requested):
    report_id, key, _ = _original(connection, tmp_path, phase)
    monkeypatch.setattr(policy, "read_report_management", create_autospec(policy.read_report_management,
        side_effect=AssertionError("异阶段键不能进入当前管理状态")))
    result = _readonly(connection, report_id, requested, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.mark.parametrize("phase", ["prepare", "intent", "publish", "failure"])
def test_original_management_key_rejects_another_report(connection, tmp_path, phase):
    from ..acceptance.test_atomicity import _process
    from .test_freeze import _plan_body

    report_id, key, _ = _original(connection, tmp_path, phase)
    assert _process(_plan_body("2"), connection).kind is DbOutcomeKind.COMPLETED
    second = _completed(policy.ReportingRepository().freeze_report(new_operation_key(), _owned(connection))).report
    assert second.report_id != report_id
    result = _readonly(connection, second.report_id, phase, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.mark.parametrize("phase", ["prepare", "failure"])
def test_unchanged_fact_in_original_delta_still_rejects_different_input(connection, tmp_path, phase):
    report_id, _, _ = _original(connection, tmp_path, "prepare")
    _completed(_call(connection, report_id, "failure", new_operation_key()))
    if phase == "failure":
        _completed(_call(connection, report_id, "intent", new_operation_key()))
    key = new_operation_key()
    _completed(_call(connection, report_id, phase, key))
    _completed(_publish(connection, report_id))
    inputs = ({"contents": _bytes(b"different\n")} if phase == "prepare"
              else {"error": {"reason": "report file", "context": {"ready": 1, "count": 1}}})
    result = _readonly(connection, report_id, phase, key, **inputs)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.mark.parametrize("mutation", ["head", "gap", "floor", "value", "before_state", "member"])
def test_management_reuse_rejects_unreliable_original_or_restore_history(connection, tmp_path, mutation):
    report_id, key, _ = _original(connection, tmp_path, "intent")
    with closing(connection.execute("SELECT last_event_id FROM history_transactions WHERE operation_key = ?", (str(key),))) as cursor:
        original_event_id = cursor.fetchone()[0]
    _completed(_call(connection, report_id, "failure", new_operation_key(), error={"index": 1}))
    with closing(connection.execute("SELECT last_event_id FROM reports WHERE id = ?", (report_id,))) as cursor:
        middle = cursor.fetchone()[0]
    _completed(_call(connection, report_id, "failure", new_operation_key(), error={"index": 2}))
    if mutation == "head":
        connection.execute("UPDATE reports SET last_event_id = ? WHERE id = ?", (middle, report_id))
    elif mutation in ("gap", "floor"):
        event_id = middle if mutation == "gap" else original_event_id
        connection.execute("DELETE FROM entity_event_links WHERE event_id = ?", (event_id,))
    elif mutation == "value":
        connection.execute("UPDATE reports SET last_error_json = ? WHERE id = ?", ('{"index":99}', report_id))
    elif mutation == "member":
        connection.execute("DELETE FROM history_events WHERE id = ?", (middle,))
    else:
        with closing(connection.execute("SELECT body_json FROM history_events WHERE id = ?", (original_event_id,))) as cursor:
            body = json.loads(cursor.fetchone()[0])
        body["rows"][0]["before"]["values"]["status"] = 4
        connection.execute("UPDATE history_events SET body_json = ? WHERE id = ?", (json.dumps(body), original_event_id))
    result = _readonly(connection, report_id, "intent", key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.mark.parametrize("missing", ["stage", "error"])
def test_formal_failure_requires_full_failed_state_and_error(connection, tmp_path, missing):
    report_id, _, _ = _original(connection, tmp_path, "prepare")
    before = tuple(connection.iterdump())
    result = _update(connection, report_id, 5,
                     {"last_error_json": ERROR} if missing == "stage" else {"status": 5})
    assert result.kind == "rolled_back"
    from camctl.history.validators import EventValidationError
    assert isinstance(result.error, EventValidationError), result.error
    assert tuple(connection.iterdump()) == before
