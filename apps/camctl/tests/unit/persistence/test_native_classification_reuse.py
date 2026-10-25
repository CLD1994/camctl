"""原生成员分类成功后只在同一事务及完整输入下复用小型结果。"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture import native_segments
from camctl.capture.media import RecordingFailure
from camctl.capture.segment_models import CurrentSegmentResult
from camctl.contracts.enums import enum_for
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError
from camctl.persistence.read_reuse import clear_read_reuse, read_reuse_scope
from camctl.persistence.repositories import segment_native, segment_result_sets, segments


@pytest.fixture
def classification(monkeypatch):
    db = create_autospec(sqlite3.Connection, instance=True)
    db.in_transaction = True
    db.total_changes = 0
    row = dict(action_id=1, id=1, version=1, run_id=1, source_kind=1, activity_id=1)
    result = native_segments.NativeClassification(CurrentSegmentResult.UNCONFIRMED, 0, None, None)
    state = SimpleNamespace(db=db, row=row, result=result, error=None)

    def patch(module, name, **options):
        port = create_autospec(getattr(module, name), **options)
        monkeypatch.setattr(module, name, port)
        return port

    def classify(members, **conditions):
        if state.error is not None:
            raise state.error
        assert tuple(members()) == ()
        assert tuple(members()) == ()
        return state.result

    state.latest = patch(segment_result_sets, "_one", return_value=None)
    state.members = patch(segments, "read_segment_members", return_value=Page((), None))
    state.classify = patch(native_segments, "classify_native_members", side_effect=classify)
    return state


def test_same_input_reuses_verified_classification(classification):
    s = classification
    with read_reuse_scope(s.db):
        first = segment_native.native_classification(s.db, s.row)
        assert segment_native.native_classification(s.db, {**s.row, "state": 2}) == first
    assert first == s.result
    assert s.classify.call_count == 1
    assert s.members.call_count == 2
    s.latest.assert_called_once()


@pytest.mark.parametrize("field", ["action_id", "id", "version", "run_id", "source_kind", "activity_id"])
def test_every_classification_input_has_distinct_identity(classification, field):
    s = classification
    with read_reuse_scope(s.db):
        segment_native.native_classification(s.db, s.row)
        segment_native.native_classification(s.db, {**s.row, field: 2})
    assert s.classify.call_count == 2


def test_integer_and_boolean_inputs_do_not_share_classification(classification):
    s = classification
    with read_reuse_scope(s.db):
        segment_native.native_classification(s.db, s.row)
        segment_native.native_classification(s.db, {**s.row, "source_kind": True})
    assert s.classify.call_count == 2


@pytest.mark.parametrize("reset", ["write", "clear", "scope"])
def test_changed_generation_or_phase_reclassifies(classification, reset):
    s = classification
    with read_reuse_scope(s.db):
        segment_native.native_classification(s.db, s.row)
        if reset == "write":
            s.db.total_changes += 1
        elif reset == "clear":
            clear_read_reuse(s.db)
        if reset != "scope":
            segment_native.native_classification(s.db, s.row)
    if reset == "scope":
        with read_reuse_scope(s.db):
            segment_native.native_classification(s.db, s.row)
    assert s.classify.call_count == 2


@pytest.mark.parametrize("error", [ConsistencyError("成员不完整"), KeyboardInterrupt()])
def test_failed_classification_is_retried_with_original_error(classification, error):
    s = classification
    s.error = error
    with read_reuse_scope(s.db):
        with pytest.raises(type(error)) as caught:
            segment_native.native_classification(s.db, s.row)
        assert caught.value is error
        s.error = None
        assert segment_native.native_classification(s.db, s.row) == s.result
        assert segment_native.native_classification(s.db, s.row) == s.result
    assert s.classify.call_count == 2


def test_outside_scope_keeps_original_classification(classification):
    s = classification
    segment_native.native_classification(s.db, s.row)
    segment_native.native_classification(s.db, s.row)
    assert s.classify.call_count == 2


def test_second_member_pass_failure_is_not_cached(classification):
    s = classification
    error = sqlite3.OperationalError("第二遍成员读取失败")
    s.members.side_effect = [Page((), None), error]
    with read_reuse_scope(s.db):
        with pytest.raises(sqlite3.OperationalError) as caught:
            segment_native.native_classification(s.db, s.row)
        assert caught.value is error
        s.members.side_effect = None
        assert segment_native.native_classification(s.db, s.row) == s.result
        assert segment_native.native_classification(s.db, s.row) == s.result
    assert s.classify.call_count == 2


def test_new_attempt_after_write_rechecks_explicit_failure(classification, monkeypatch):
    s = classification
    facts = create_autospec(segment_native.row_facts, return_value={
        "status": int(enum_for("operation_attempts.status").FAILED),
        "error_json": {"code": "device_error"},
    })
    monkeypatch.setattr(segment_native, "row_facts", facts)
    with read_reuse_scope(s.db):
        segment_native.native_classification(s.db, s.row)
        assert s.classify.call_args.kwargs["explicit_error"] is False
        s.latest.return_value = (9,)
        s.db.total_changes += 1
        segment_native.native_classification(s.db, s.row)
        assert s.classify.call_args.kwargs["explicit_error"] is True
    facts.assert_called_once_with(s.db, "operation_attempts", 9)


def test_cached_classification_has_independent_failure_details(classification):
    s = classification
    s.result = native_segments.NativeClassification(CurrentSegmentResult.UNMET, 0, None,
        RecordingFailure("capture_failed", {"activity_id": "1", "reason": "device_failed"}))
    with read_reuse_scope(s.db):
        first = segment_native.native_classification(s.db, s.row)
        first.failure.details["reason"] = "changed"
        second = segment_native.native_classification(s.db, s.row)
        assert second.failure.details["reason"] == "device_failed"
        second.failure.details["reason"] = "changed_again"
        assert segment_native.native_classification(s.db, s.row).failure.details["reason"] == "device_failed"
    assert s.classify.call_count == 1
