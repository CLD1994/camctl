"""形成原生结论时真实分类；普通消费使用独立保存事实接口。"""

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


def test_forming_each_conclusion_classifies_actual_members(classification):
    s = classification
    assert segment_native.native_classification(s.db, s.row) == s.result
    assert segment_native.native_classification(s.db, s.row) == s.result
    assert s.classify.call_count == 2
    assert s.members.call_count == 4


@pytest.mark.parametrize("error", [ConsistencyError("成员不完整"), KeyboardInterrupt()])
def test_forming_failure_propagates_and_next_request_retries(classification, error):
    s = classification
    s.error = error
    with pytest.raises(type(error)) as caught:
        segment_native.native_classification(s.db, s.row)
    assert caught.value is error
    s.error = None
    assert segment_native.native_classification(s.db, s.row) == s.result
    assert s.classify.call_count == 2


def test_forming_conclusion_checks_new_attempt_explicit_error(classification, monkeypatch):
    s = classification
    facts = create_autospec(segment_native.row_facts, return_value={
        "status": int(enum_for("operation_attempts.status").FAILED),
        "error_json": {"code": "device_error"}})
    monkeypatch.setattr(segment_native, "row_facts", facts)
    segment_native.native_classification(s.db, s.row)
    assert s.classify.call_args.kwargs["explicit_error"] is False
    s.latest.return_value = (9,)
    segment_native.native_classification(s.db, s.row)
    assert s.classify.call_args.kwargs["explicit_error"] is True
    facts.assert_called_once_with(s.db, "operation_attempts", 9)


def test_second_actual_member_pass_failure_propagates(classification):
    s = classification
    error = sqlite3.OperationalError("第二遍成员读取失败")
    s.members.side_effect = [Page((), None), error]
    with pytest.raises(sqlite3.OperationalError) as caught:
        segment_native.native_classification(s.db, s.row)
    assert caught.value is error
