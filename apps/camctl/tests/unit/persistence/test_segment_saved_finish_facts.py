"""普通收场消费固定结论，事件生成只接收已读取的有限事实。"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.media import RecordingFailure
from camctl.capture.segment_finish import FinishSegmentRecording
from camctl.capture.segment_models import CurrentSegmentResult, SegmentAssessmentStatus
from camctl.capture.segment_storage import SegmentResultRef
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories import segment_check_conclusions as conclusions
from camctl.persistence.repositories import segment_native, segment_transactions as finish


def port(monkeypatch, module, name, **kwargs):
    value = create_autospec(getattr(module, name), **kwargs)
    monkeypatch.setattr(module, name, value)
    return value


@pytest.mark.parametrize("result", list(CurrentSegmentResult))
def test_saved_native_result_does_not_reclassify_members(monkeypatch, result):
    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    failure = RecordingFailure("capture_failed", {"activity_id": "3", "reason": "device_failed"})
    saved = {"reason": int(enum_for("segment_check_conclusions.reason").NATIVE_RESULT_DECIDED),
             "current_result": int(enum_for("segment_check_conclusions.current_result")[result.name]),
             "current_failure_json": {"code": failure.code, "details": failure.details}}
    read = port(monkeypatch, conclusions, "read_fixed_result_check_conclusion", return_value=saved)
    classify = port(monkeypatch, segment_native, "native_classification", side_effect=AssertionError("重跑旧分类"))
    actual = segment_native.read_saved_native_classification(db, {"id": 2})
    assert actual.current_result is result
    assert actual.failure == failure
    assert actual.status is (SegmentAssessmentStatus.SUCCEEDED if result is CurrentSegmentResult.SUFFICIENT
                             else SegmentAssessmentStatus.FAILED if result is CurrentSegmentResult.UNMET
                             else SegmentAssessmentStatus.UNCONFIRMED)
    read.assert_called_once_with(db, {"id": 2})
    classify.assert_not_called()
    db.execute.assert_not_called()


def test_saved_native_result_rejects_other_conclusion(monkeypatch):
    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    port(monkeypatch, conclusions, "read_fixed_result_check_conclusion", return_value={
        "reason": int(enum_for("segment_check_conclusions.reason").NO_RESULT_WORK)})
    with pytest.raises(ConsistencyError):
        segment_native.read_saved_native_classification(db, {"id": 2})


def test_canonical_empty_finish_uses_only_supplied_facts(monkeypatch):
    request = FinishSegmentRecording(SegmentResultRef(1, 2, 1), 3, 4, 5)
    tx = TransactionRange(7, 10, 11)
    header = {"action_status": 3, "first_output_id": 1, "first_origin_id": 1,
              "original_count": 0, "preview_count": 0, "scope_count": 0}
    qualification = {"action_status": 2, "finish_run": False, "complete_plan": False,
                     "plan_result_status": 2, "plan_status": 2, "preparation_events": ()}
    header.update(finish_run=False, complete_plan=False, plan_status=2)
    facts = finish._FinishFacts({"plan_id": 6}, {}, {"id": 2}, None, (3, None, None))
    port(monkeypatch, finish, "_identity", side_effect=AssertionError("生成时读取数据库"))
    port(monkeypatch, finish, "_failure", side_effect=AssertionError("生成时证明来源"))
    events = tuple(finish._canonical_events(request, tx, header, facts, qualification,
                   lambda: iter(()), lambda: iter(()), lambda *_: pytest.fail("没有前序产物")))
    assert [event.event_id for event in events] == [10, 11]
    assert events[0].rows[0].after.values["status"] == 3
    assert events[-1].rows[0].after.values["published_event_id"] == 11


def test_finish_plan_does_not_reprove_saved_assessment(monkeypatch):
    from camctl.capture.segment_models import DefaultScopeState
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.transaction import TransactionScope, update_change

    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    scope = TransactionScope(db, 6, 9)
    request = FinishSegmentRecording(SegmentResultRef(1, 2, 1), 3, 4, 5)
    assessment = SimpleNamespace(default_scope=SimpleNamespace(state=DefaultScopeState.UNKNOWN, member_count=None))
    port(monkeypatch, finish, "_one", return_value=None)
    port(monkeypatch, finish, "_identity", return_value=({"status": 2, "cancel_requested": 0, "plan_id": 6}, {},
         {"id": 2, "published_event_id": None}, assessment))
    port(monkeypatch, finish, "_canceled_result", return_value=False)
    port(monkeypatch, finish, "_candidates", return_value=iter(()))
    port(monkeypatch, finish, "_scopes", return_value=iter(()))
    port(monkeypatch, finish, "_verify_scope_members")
    port(monkeypatch, finish, "row_facts", return_value={"id": 6, "status": 2})
    port(monkeypatch, finish, "_failure", return_value=(3, None, None))
    qualification = {"action_status": 2, "finish_run": False, "complete_plan": False,
                     "plan_result_status": 2, "plan_status": 2, "preparation_events": ()}
    port(monkeypatch, finish, "_completion_qualification", return_value=qualification)
    port(monkeypatch, finish.storage, "segment_activation_rows", return_value=(
        update_change("segment_sets", 2, {"published_event_id": None}, {"published_event_id": 11}),))
    proof = port(monkeypatch, finish, "verify_completion_proof", side_effect=AssertionError("重演旧来源"))
    classification = port(monkeypatch, finish, "_verify_assessment", side_effect=AssertionError("重算旧判断"))
    actual = finish._FinishCommand(request, new_operation_key(), 128).plan(scope)
    assert actual.result.action_status == 3
    assert actual.result.output_count == 0
    proof.assert_not_called()
    classification.assert_not_called()


@pytest.mark.parametrize("wrong_original", [False, True])
def test_canonical_orders_originals_before_preview_and_uses_actual_mapping(wrong_original):
    from camctl.capture.segment_storage import SegmentOutputCandidate
    from camctl.outputs.catalog import OutputKind

    request = FinishSegmentRecording(SegmentResultRef(1, 2, 1), 3, 4, 5)
    # 129 originals cross the production page limit; preview appears first in the input.
    header = {"action_status": 3, "first_output_id": 20, "first_origin_id": 1,
              "original_count": 129, "preview_count": 1, "scope_count": 0,
              "finish_run": False, "complete_plan": False, "plan_status": 2}
    qualification = {"action_status": 2, "finish_run": False, "complete_plan": False,
                     "plan_result_status": 2, "plan_status": 2, "preparation_events": ()}
    facts = finish._FinishFacts({"plan_id": 6}, {}, {"id": 2}, None, (3, None, None))
    file = {"original_name": "video.osv", "media_type": "video/x-osv"}
    def candidates():
        yield SegmentOutputCandidate(130, 130, OutputKind.PREVIEW, 1, 1), file
        for identity in range(1, 130):
            yield SegmentOutputCandidate(identity, identity, OutputKind.ORIGINAL), file
    seen = []
    def created(file_id, kind):
        assert file_id == 1
        assert len(seen) == 129
        if wrong_original:
            raise ConsistencyError("实际前序原片缺失")
        return 20
    events = finish._canonical_events(request, TransactionRange(7, 10, 141), header, facts,
                                     qualification, candidates, lambda: iter(()), created)
    assert next(events).event_id == 10
    for _ in range(129):
        output = next(events)
        assert output.rows[0].after.values["kind"] == int(enum_for("outputs.kind").ORIGINAL)
        seen.append(output.rows[0].row_id)
    if wrong_original:
        with pytest.raises(ConsistencyError, match="实际前序原片缺失"):
            next(events)
    else:
        preview = next(events)
        assert preview.rows[1].after.values["original_output_id"] == 20
        assert next(events).rows[0].after.values["published_event_id"] == 141
        with pytest.raises(StopIteration):
            next(events)


@pytest.mark.parametrize("defect", [None, "missing", "foreign_set", "changed_version", "other_branch"])
def test_fixed_conclusion_checks_its_direct_creation_version(monkeypatch, defect):
    from camctl.history.events import business_columns
    from camctl.persistence import fact_reads
    from camctl.persistence.transaction import event_envelope, row_change

    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    values = {column: None for column in business_columns("segment_check_conclusions")}
    values.update(set_id=2, reason=int(enum_for("segment_check_conclusions.reason").NATIVE_RESULT_DECIDED))
    saved = {"id": 8, "created_event_id": 7, **values}
    event = event_envelope(7, 3, conclusions._EVENT["id"], conclusions._REASON,
                           (row_change("segment_check_conclusions", 8, values),), 5)
    if defect == "foreign_set":
        saved["set_id"] = 99
    elif defect == "changed_version":
        saved["anchor_event_id"] = 99
    elif defect == "other_branch":
        from dataclasses import replace
        event = replace(event, reason=conclusions._EVENT["branches"]["DECIDE_RESULT"]["reason"])
    port(monkeypatch, conclusions, "_one", return_value=None if defect == "missing" else (8,))
    port(monkeypatch, conclusions, "row_facts", return_value=saved)
    port(monkeypatch, fact_reads, "read_event", return_value=fact_reads.EventRecord((), event, TransactionRange(3, 7, 7)))
    old = port(monkeypatch, conclusions, "read_verified_conclusion", side_effect=AssertionError("重演原申请"))
    if defect is None:
        assert conclusions.read_fixed_result_check_conclusion(db, {"id": 2}) == saved
    else:
        with pytest.raises(ConsistencyError):
            conclusions.read_fixed_result_check_conclusion(db, {"id": 2})
    old.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.parametrize("forged", [False, True])
def test_seal_independently_uses_guard_facts_and_closes_progress(monkeypatch, forged):
    from dataclasses import replace
    from camctl.capture.segment_finish import request_document
    from camctl.history.reads import PlanReadScope
    from camctl.history.validators import ValidatedEvent
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.persistence.transaction import update_change

    request = FinishSegmentRecording(SegmentResultRef(1, 2, 1), 3, 4, 5)
    tx = TransactionRange(7, 10, 11)
    header = {"request": request_document(request), "action_status": 3,
              "first_output_id": 1, "first_origin_id": 1, "output_count": 0,
              "original_count": 0, "preview_count": 0, "scope_count": 0, "batch_size": 128,
              "finish_run": False, "complete_plan": False, "plan_status": 2}
    qualification = {"action_status": 2, "finish_run": False, "complete_plan": False,
                     "plan_result_status": 2, "plan_status": 2, "preparation_events": ()}
    facts = finish._FinishFacts({"plan_id": 6}, {}, {"id": 2}, None, (3, None, None))
    first, marker = tuple(finish._canonical_events(request, tx, header, facts, qualification,
        lambda: iter(()), lambda: iter(()), lambda *_: pytest.fail("没有前序产物")))
    if forged:
        first = replace(first, rows=(update_change("actions", 1, {"status": 2}, {"status": 4}),))
    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    reads = SqlEventReads(db, PlanReadScope(6, 128))
    state = {}
    reads.bind_segment_finish_state(state, tx)
    port(monkeypatch, finish, "_finish_event_inputs", return_value=(lambda: iter(()), lambda: iter(())))
    port(monkeypatch, SqlEventReads, "transaction_event", return_value=marker)
    reads.prepare_segment_finish(first, tx, header, qualification, facts)
    validated = ValidatedEvent(first, "ACTION_FINISHED", "SUCCEED", (), {})
    if forged:
        with pytest.raises(ConsistencyError, match="完整规范事件不同"):
            reads.seal_segment_finish(validated)
    else:
        reads.seal_segment_finish(validated)
        assert "progress" in state
        reads.seal_segment_finish(ValidatedEvent(marker, "SEGMENT_FACTS", "ACTIVATE", (), {}))
        assert "progress" not in state
    db.execute.assert_not_called()


@pytest.mark.parametrize("defect", [None, "missing_last", "extra_event", "changed_first"])
def test_original_key_verification_consumes_whole_exact_event_group(monkeypatch, defect):
    from dataclasses import replace
    from camctl.capture.segment_finish import request_document
    from camctl.capture.segment_models import DefaultScopeState
    from camctl.contracts.pages import Page
    from camctl.contracts.values import new_operation_key

    request = FinishSegmentRecording(SegmentResultRef(1, 2, 1), 3, 4, 5)
    tx = TransactionRange(7, 10, 11)
    header = {"request": request_document(request), "action_status": 3,
              "first_output_id": 1, "first_origin_id": 1, "output_count": 0,
              "original_count": 0, "preview_count": 0, "scope_count": 0, "batch_size": 128,
              "finish_run": False, "complete_plan": False, "plan_status": 2}
    qualification = {"action_status": 2, "finish_run": False, "complete_plan": False,
                     "plan_result_status": 2, "plan_status": 2, "preparation_events": ()}
    assessment = SimpleNamespace(default_scope=SimpleNamespace(state=DefaultScopeState.UNKNOWN, member_count=None))
    facts = finish._FinishFacts({"plan_id": 6}, {}, {"id": 2}, assessment, (3, None, None))
    first, marker = tuple(finish._canonical_events(request, tx, header, facts, qualification,
        lambda: iter(()), lambda: iter(()), lambda *_: pytest.fail("没有前序产物")))
    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    port(monkeypatch, finish, "_identity", return_value=(facts.action, facts.run, facts.setrow, assessment))
    port(monkeypatch, finish, "_completion_qualification", return_value=qualification)
    port(monkeypatch, finish, "_verify_scope_members")
    port(monkeypatch, finish, "_failure", return_value=facts.outcome)
    port(monkeypatch, finish, "_finish_event_inputs", return_value=(lambda: iter(()), lambda: iter(())))
    port(monkeypatch, finish, "verify_completion_proof", side_effect=AssertionError("重演旧来源"))
    port(monkeypatch, finish, "_verify_assessment", side_effect=AssertionError("重算旧判断"))
    tail = () if defect == "missing_last" else (marker,)
    if defect == "extra_event":
        tail += (marker,)
    if defect == "changed_first":
        first = replace(first, occurred_at=6)
    page = port(monkeypatch, finish, "read_saved_transaction_page", return_value=Page(tail, None))
    reader = SimpleNamespace(connection=db)
    if defect is None:
        actual = finish._verify_events(request, new_operation_key(), tx, header, reader, 128, Page((first,), 10))
        assert actual.action_status == 3
        assert actual.boundary.last_event_id == 11
    else:
        with pytest.raises(ConsistencyError, match="完整事件"):
            finish._verify_events(request, new_operation_key(), tx, header, reader, 128, Page((first,), 10))
    if defect != "changed_first":
        page.assert_called_once()
    db.execute.assert_not_called()


@pytest.mark.parametrize("expected,actual", [(None, 0), (0, 0), (2, 2), (None, 1), (2, 1), (2, 3)])
def test_saved_gap_count_checks_direct_completeness(monkeypatch, expected, actual):
    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    port(monkeypatch, finish, "_one", return_value=(actual,))
    assessment = SimpleNamespace(gap_count=expected)
    if actual == (expected or 0):
        finish._verify_saved_gap_count(db, {"id": 2}, assessment)
    else:
        with pytest.raises(ConsistencyError, match="缺口数量"):
            finish._verify_saved_gap_count(db, {"id": 2}, assessment)


@pytest.mark.parametrize("status,error,details", [(3, None, None), (6, None, None),
    (4, 1, {"activity_id": "3", "reason": "outputs_unknown"})])
def test_canonical_keeps_failure_and_cancel_terminal_fields(status, error, details):
    request = FinishSegmentRecording(SegmentResultRef(1, 2, 1), 3, 4, 5)
    header = {"action_status": status, "first_output_id": 1, "first_origin_id": 1,
              "original_count": 0, "preview_count": 0, "scope_count": 0,
              "finish_run": False, "complete_plan": False, "plan_status": 2}
    qualification = {"action_status": 2, "finish_run": False, "complete_plan": False,
                     "plan_result_status": 2, "plan_status": 2, "preparation_events": ()}
    facts = finish._FinishFacts({"plan_id": 6}, {}, {"id": 2}, None, (status, error, details))
    events = finish._canonical_events(request, TransactionRange(7, 10, 11), header, facts,
                                     qualification, lambda: iter(()), lambda: iter(()), lambda *_: None)
    values = next(events).rows[0].after.values
    assert values == ({"status": 4, "error_code": error, "error_details_json": details}
                      if status == 4 else {"status": status})
    assert next(events).rows[0].after.values == {"published_event_id": 11}


@pytest.mark.parametrize("has_current,error", [(False, None), (True, None),
    (False, ConsistencyError("原有限结论错误")), (True, ConsistencyError("原STOP来源错误"))])
def test_assessment_formation_proves_actual_sources_without_saved_assessment(monkeypatch, has_current, error):
    from camctl.capture.segment_models import SegmentSetRef

    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    row = {"id": 2, "activity_id": 3, "run_id": 4, "assessment_json": None}
    facts = object()
    ref = SegmentSetRef(1, 8, 9, 1) if has_current else SegmentResultRef(1, 2, 1)
    port(monkeypatch, finish.storage, "_ref", return_value=ref)
    source = port(monkeypatch, finish, "verify_completion_proof", side_effect=error if has_current else None)
    conclusion = port(monkeypatch, conclusions, "read_verified_conclusion", side_effect=error if not has_current else None)
    reader = SimpleNamespace(connection=db)
    if error is None:
        finish.verify_assessment_sources(row, facts, reader)
    else:
        with pytest.raises(ConsistencyError) as caught:
            finish.verify_assessment_sources(row, facts, reader)
        assert caught.value is error
    if has_current:
        source.assert_called_once()
        assert source.call_args.kwargs["facts"] is facts
        assert source.call_args.args[0].ref == ref
        conclusion.assert_not_called()
    else:
        conclusion.assert_called_once_with(db, row)
        source.assert_not_called()
    db.execute.assert_not_called()
