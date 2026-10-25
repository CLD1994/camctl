"""分段准备失败只消费最新原调用，不借用旧 START 或设置结果。"""
from types import SimpleNamespace

import pytest

from camctl.capture.segment_storage import (
    CurrentSegmentResult, PublicationKind, ResultCheckConclusionSave,
    ResultCheckEndReason, SegmentResultRef, SegmentSourceKind, encode_enum,
)
from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories import segment_check_conclusions as storage
from camctl.persistence import transaction
from camctl.history import decoding
from camctl.history.events import business_columns


@pytest.fixture
def preparation_source(monkeypatch):
    action = dict(id=7, status=2, execution_started=1, cancel_requested=0)
    activity = dict(id=8, action_id=7, occupancy_state=1, dispatch_state=1)
    run = dict(id=10, action_id=7, activity_id=8, kind=10, status=4,
               attempts_used=1, error_json={"code": "device_error"})
    attempt = dict(id=11, run_id=10, attempt_no=1, status=3,
                   intent_event_id=16, result_event_id=20,
                   error_json={"code": "device_error"}, result_json={"format_version": 1})
    setrow = dict(id=12, action_id=7, activity_id=8, run_id=None, version=1,
                  source_kind=encode_enum(SegmentSourceKind.NO_RESULTS),
                  publication_kind=encode_enum(PublicationKind.RESULT), state=2,
                  member_count=0, source_txn_id=2, source_event_id=20)
    facts = SimpleNamespace(not_started=True, preparation_resolved=True, activity=activity)
    state = SimpleNamespace(action=action, activity=activity, run=run, attempt=attempt,
                            setrow=setrow, facts=facts, preparation_runs=[(10, 16)], start=None,
                            results=None, product=None, baseline=None)
    state.event = SimpleNamespace(event_type=storage.load_event_registry()["events"]["ATTEMPT_RESULT"]["id"],
        transaction_id=2, rows=(SimpleNamespace(table="operation_attempts", row_id=11,
            after=SimpleNamespace(values={key: attempt[key] for key in
                ("status", "result_event_id", "error_json", "result_json")})),))
    state.transaction = SimpleNamespace(first_event_id=19, last_event_id=20)
    def one(db, sql, parameters=()):
        if "FROM history_events" in sql:
            return state.event
        if "JOIN operation_attempts" in sql and "result_event_id" in sql:
            return (10,) if parameters[-1] == 20 else None
        if "ORDER BY" in sql:
            last_event = parameters[2] if len(parameters) == 3 else float("inf")
            return (max(identity for identity, intent in state.preparation_runs if intent <= last_event),)
        if "FROM operation_attempts" in sql:
            return (11,)
        if "FROM outputs" in sql:
            return state.product
        if "FROM baseline_read_calls" in sql:
            return state.baseline
        if "FROM operation_runs" in sql:
            return state.start if parameters[-1] == 1 else state.results
        raise AssertionError(sql)
    monkeypatch.setattr(storage, "_one", one)
    rows = {"actions": action, "device_activities": activity,
            "operation_runs": run, "operation_attempts": attempt, "segment_sets": setrow}
    for table, row in rows.items():
        assert set(row) <= {"id", *business_columns(table)}, table
    monkeypatch.setattr(storage, "row_facts", lambda db, table, identity: rows[table])
    monkeypatch.setattr(storage, "_ended_attempts", lambda db, row, boundary: 20)
    from camctl.persistence.repositories import capture_facts
    monkeypatch.setattr(capture_facts, "load_start_facts", lambda db, row: facts)
    from camctl.persistence.repositories import segment_result_sets
    monkeypatch.setattr(segment_result_sets, "_check_boundary", lambda db, boundary: boundary)
    monkeypatch.setattr(segment_result_sets, "_row_at", lambda db, table, *args: rows[table])
    monkeypatch.setattr(decoding, "decode_event_row", lambda raw: raw)
    monkeypatch.setattr(transaction, "read_transaction_range", lambda db, identity: state.transaction)
    return state


def _request():
    return ResultCheckConclusionSave(SegmentResultRef(7, 12, 1),
        ResultCheckEndReason.NO_RESULT_WORK, 20, None, None, None, None, 100)


@pytest.mark.parametrize("old_start", [None, (3,)])
def test_no_results_uses_preparation_anchor_before_old_start(preparation_source, old_start):
    preparation_source.start = old_start
    result = storage._values(None, _request(), preparation_source.setrow)
    assert result["current_result"] == encode_enum(CurrentSegmentResult.NONE)
    assert result["current_failure_json"] == {
        "code": "capture_failed", "details": {"activity_id": "8", "reason": "device_failed"}}


def test_unconfirmed_preparation_keeps_device_failure_without_a_start(preparation_source):
    preparation_source.run["status"] = 6
    preparation_source.attempt["status"] = 4
    preparation_source.event.rows[0].after.values["status"] = 4
    result = storage._values(None, _request(), preparation_source.setrow)
    assert result["current_failure_json"] == {
        "code": "capture_failed", "details": {"activity_id": "8", "reason": "device_failed"}}


@pytest.mark.parametrize("target,change", [
    ("run", {"status": 3}), ("attempt", {"status": 2}),
    ("attempt", {"error_json": None}), ("attempt", {"result_event_id": 21}),
    ("attempt", {"intent_event_id": None}), ("attempt", {"intent_event_id": 21}),
    ("activity", {"occupancy_state": 2}), ("run", {"activity_id": 9}),
])
def test_preparation_anchor_rejects_unreliable_source(preparation_source, target, change):
    getattr(preparation_source, target).update(change)
    with pytest.raises(ConsistencyError):
        storage.no_result_preparation_failure(None, preparation_source.setrow, 20)


@pytest.mark.parametrize("field,value", [
    ("preparation_runs", [(10, 16), (14, 18)]),
    ("product", (9,)), ("baseline", (9,)), ("results", (9,)),
])
def test_preparation_failure_cannot_use_stale_or_nonempty_work(preparation_source, field, value):
    setattr(preparation_source, field, value)
    with pytest.raises(ConsistencyError):
        storage.no_result_preparation_failure(None, preparation_source.setrow, 20)


@pytest.mark.parametrize("field", ["not_started", "preparation_resolved"])
def test_preparation_failure_waits_for_original_effect_and_call_settlement(preparation_source, field):
    setattr(preparation_source.facts, field, False)
    with pytest.raises(ConsistencyError):
        storage.no_result_preparation_failure(None, preparation_source.setrow, 20)


@pytest.mark.parametrize("field,value", [("error_json", {"code": "another_error"}),
    ("result_event_id", 19), ("status", 2), ("result_json", {})])
def test_preparation_failure_requires_same_actual_attempt_event(preparation_source, field, value):
    preparation_source.event.rows[0].after.values[field] = value
    with pytest.raises(ConsistencyError):
        storage.no_result_preparation_failure(None, preparation_source.setrow, 20)


def test_preparation_failure_cannot_cross_original_complete_transaction(preparation_source):
    preparation_source.transaction.last_event_id = 21
    with pytest.raises(ConsistencyError):
        storage.no_result_preparation_failure(None, preparation_source.setrow, 20)


def test_saved_preparation_failure_rechecks_the_same_anchor(preparation_source, monkeypatch):
    monkeypatch.setattr(storage, "read_result_set", lambda ref, owned: preparation_source.setrow)
    conclusion = {"current_failure_json": {
        "code": "capture_failed", "details": {"activity_id": "8", "reason": "device_failed"}}}
    storage._verify_saved_prefix(None, _request(), ({},), conclusion)
    preparation_source.preparation_runs.append((14, 18))
    with pytest.raises(ConsistencyError):
        storage._verify_saved_prefix(None, _request(), ({},), conclusion)


def test_later_preparation_does_not_change_the_original_source(preparation_source):
    preparation_source.preparation_runs.append((14, 21))
    result = storage._values(None, _request(), preparation_source.setrow)
    assert result["current_failure_json"] == {
        "code": "capture_failed", "details": {"activity_id": "8", "reason": "device_failed"}}


def test_saved_preparation_failure_cannot_drop_the_business_error(preparation_source, monkeypatch):
    monkeypatch.setattr(storage, "read_result_set", lambda ref, owned: preparation_source.setrow)
    with pytest.raises(ConsistencyError):
        storage._verify_saved_prefix(None, _request(), ({},), {"current_failure_json": None})


def test_preparation_failure_publishes_the_original_business_error(preparation_source, monkeypatch):
    from camctl.capture.media import RecordingFailure
    from camctl.persistence.repositories import segment_dispositions
    from unittest.mock import create_autospec
    monkeypatch.setattr(segment_dispositions, "saved_result_disposition", create_autospec(
        segment_dispositions.saved_result_disposition, return_value=None))
    monkeypatch.setattr(storage, "read_fixed_result_check_conclusion", create_autospec(
        storage.read_fixed_result_check_conclusion, return_value={
        "reason": encode_enum(ResultCheckEndReason.NO_RESULT_WORK), "anchor_event_id": 20,
        "current_failure_json": {"code": "capture_failed",
            "details": {"activity_id": "8", "reason": "device_failed"}}}))
    request = SimpleNamespace(ref=_request().ref, failure_event_id=None,
        failure=RecordingFailure("capture_failed", {"activity_id": "8", "reason": "device_failed"}))
    result = segment_dispositions.saved_result_failure(request, preparation_source.setrow, storage.read_fixed_result_check_conclusion(None, preparation_source.setrow))
    assert result[0] == 4
    assert result[2] == {"activity_id": "8", "reason": "device_failed"}
