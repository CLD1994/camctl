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
         {"id": 2, "published_event_id": None}, assessment, None))
    port(monkeypatch, finish, "_canceled_result", return_value=False)
    port(monkeypatch, finish, "_candidates", return_value=iter(()))
    port(monkeypatch, finish, "_scopes", return_value=iter(()))
    port(monkeypatch, finish, "_verify_scope_members")
    port(monkeypatch, finish, "row_facts", return_value={"id": 6, "status": 2})
    port(monkeypatch, finish, "_failure", return_value=(3, None, None))
    port(monkeypatch, finish, "_read_failure_sources", return_value=finish._FailureSources())
    qualification = {"action_status": 2, "finish_run": False, "complete_plan": False,
                     "plan_result_status": 2, "plan_status": 2, "preparation_events": ()}
    port(monkeypatch, finish, "_completion_qualification", return_value=qualification)
    port(monkeypatch, finish.storage, "segment_activation_rows", return_value=(
        update_change("segment_sets", 2, {"published_event_id": None}, {"published_event_id": 11}),))
    proof = port(monkeypatch, finish, "verify_completion_proof", side_effect=AssertionError("重演旧来源"))
    classification = port(monkeypatch, finish.storage, "_verify_assessment", side_effect=AssertionError("重算旧判断"))
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


@pytest.mark.parametrize("defect", [None, "missing", "foreign_set", "changed_version", "other_branch", "missing_event", "invalid_json"])
def test_fixed_conclusion_checks_its_direct_creation_version(monkeypatch, defect):
    from camctl.history.events import business_columns
    from camctl.persistence import fact_reads
    from camctl.persistence.transaction import event_envelope, row_change

    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    from camctl.persistence.transaction import encode_json_value
    values = {column: None for column in business_columns("segment_check_conclusions")}
    values.update(set_id=2, reason=int(enum_for("segment_check_conclusions.reason").NATIVE_RESULT_DECIDED))
    saved = {"id": 8, "created_event_id": 7, **values}
    saved["current_failure_json"] = values["current_failure_json"] = {
        "code": "capture_failed", "details": {"reason": "no_outputs"}}
    event = event_envelope(7, 3, conclusions._EVENT["id"], conclusions._REASON,
                           (row_change("segment_check_conclusions", 8, values),), 5)
    if defect == "foreign_set":
        saved["set_id"] = 99
    elif defect == "changed_version":
        saved["anchor_event_id"] = 99
    elif defect == "other_branch":
        from dataclasses import replace
        event = replace(event, reason=conclusions._EVENT["branches"]["DECIDE_RESULT"]["reason"])
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.description = tuple((name,) for name in saved)
    raw = [encode_json_value(value) if isinstance(value, dict) else value for value in saved.values()]
    if defect == "invalid_json":
        raw[list(saved).index("current_failure_json")] = "{"
    cursor.fetchone.return_value = None if defect == "missing" else tuple(raw)
    db.execute.return_value = cursor
    port(monkeypatch, fact_reads, "read_event", side_effect=ConsistencyError("创建事件缺失") if defect == "missing_event" else None,
         return_value=fact_reads.EventRecord((), event, TransactionRange(3, 7, 7)))
    old = port(monkeypatch, conclusions, "read_verified_conclusion", side_effect=AssertionError("重演原申请"))
    if defect is None:
        assert conclusions.read_fixed_result_check_conclusion(db, {"id": 2}) == saved
    else:
        with pytest.raises(ConsistencyError):
            conclusions.read_fixed_result_check_conclusion(db, {"id": 2})
    old.assert_not_called()
    # 一次集合定位同时取得真实完整行；精确 JSON 解码和创建事件核对仍保留。
    assert db.execute.call_count == 1
    assert db.execute.call_args.args == ("SELECT * FROM segment_check_conclusions WHERE set_id=?", (2,))
    cursor.close.assert_called_once()


@pytest.mark.parametrize("defect", [None, "missing", "duplicate", "before", "after", "missing_event", "wrong_type", "foreign_transaction"])
def test_created_output_reads_identity_and_actual_creation_in_one_query(defect):
    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    event_type = finish.load_event_registry()["events"]["OUTPUT_REGISTERED"]["id"]
    row = (29, 11, event_type, 7)
    if defect == "before": row = (29, 9, event_type, 7)
    elif defect == "after": row = (29, 14, event_type, 7)
    elif defect == "missing_event": row = (29, 11, None, None)
    elif defect == "wrong_type": row = (29, 11, -1, 7)
    elif defect == "foreign_transaction": row = (29, 11, event_type, 8)
    cursor.fetchmany.return_value = [] if defect == "missing" else [row, row] if defect == "duplicate" else [row]
    cursor.fetchone.return_value = (event_type, 7)
    db.execute.return_value = cursor
    if defect is None:
        assert finish._created_output(db, TransactionRange(7, 10, 13), 1, 30, 1) == 29
    else:
        with pytest.raises(ConsistencyError):
            finish._created_output(db, TransactionRange(7, 10, 13), 1, 30, 1)
    assert db.execute.call_count == 1
    assert db.execute.call_args.args[1] == (1, 30, 1)
    cursor.close.assert_called_once()


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
    port(monkeypatch, finish, "_identity", return_value=(facts.action, facts.run, facts.setrow, assessment, None))
    port(monkeypatch, finish, "_completion_qualification", return_value=qualification)
    port(monkeypatch, finish, "_verify_scope_members")
    port(monkeypatch, finish, "_failure", return_value=facts.outcome)
    port(monkeypatch, finish, "_read_failure_sources", return_value=finish._FailureSources())
    port(monkeypatch, finish, "_finish_event_inputs", return_value=(lambda: iter(()), lambda: iter(())))
    port(monkeypatch, finish, "verify_completion_proof", side_effect=AssertionError("重演旧来源"))
    port(monkeypatch, finish.storage, "_verify_assessment", side_effect=AssertionError("重算旧判断"))
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


@pytest.mark.parametrize("defect", [None, "unlinked", "foreign_action", "foreign_activity", "wrong_error", "future_anchor"])
def test_explicit_failure_rule_consumes_only_supplied_error_facts(monkeypatch, defect):
    from camctl.capture.segment_models import SegmentSetRef, SegmentAssessmentReason
    from camctl.contracts.workflow_errors import registered_error
    from camctl.persistence.transaction import update_change

    failure = RecordingFailure("capture_failed", {"activity_id": "3", "reason": "device_failed"})
    request = FinishSegmentRecording(SegmentSetRef(1, 8, 9, 1), 3, 4, 5, failure,
                                    11 if defect == "future_anchor" else 7)
    assessment = SimpleNamespace(status=SegmentAssessmentStatus.FAILED, reason=SegmentAssessmentReason.EXPLICIT_ERROR)
    port(monkeypatch, finish, "facts_from_document", return_value=SimpleNamespace(finite_check_complete=True))
    actual_error = {"code": "device_error", "stage": "device", "details": {}}
    if defect == "wrong_error":
        actual_error = {"stage": "device", "details": {}}
    change = update_change("operation_attempts", 8, {"error_json": None}, {"error_json": actual_error})
    run = {"id": 4, "action_id": 99 if defect == "foreign_action" else 1,
           "activity_id": 99 if defect == "foreign_activity" else 3,
           "kind": int(enum_for("operation_runs.kind").STOP)}
    attempt = {"status": int(enum_for("operation_attempts.status").FAILED)}
    sources = finish._FailureSources(anchor_owned=defect != "unlinked", error_rows=((change, run, attempt),))
    if defect is None:
        assert finish._failure(request, assessment, 0, {"facts_json": {}}, sources, before_event_id=10) == (
            4, registered_error(failure.code)["action_error_id"], failure.details)
    else:
        with pytest.raises(ConsistencyError):
            finish._failure(request, assessment, 0, {"facts_json": {}}, sources, before_event_id=10)


@pytest.mark.parametrize("activity_id", [None, 3])
def test_result_identity_keeps_no_activity_and_no_results_partition(monkeypatch, activity_id):
    from camctl.persistence.repositories import segment_dispositions

    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    request = FinishSegmentRecording(SegmentResultRef(1, 2, 1), activity_id, None, 5)
    identity = finish.storage.SegmentIdentity(
        {"id": 2, "activity_id": None, "run_id": None}, {"id": 1}, None, None, None)
    port(monkeypatch, finish.storage, "read_segment_identity", return_value=identity)
    port(monkeypatch, conclusions, "read_fixed_result_check_conclusion", return_value={})
    disposition = port(monkeypatch, segment_dispositions, "saved_result_disposition",
                       return_value=segment_dispositions.RetainedResultDisposition())
    port(monkeypatch, finish, "_one", return_value=(0,))
    if activity_id is None:
        assert finish._identity(request, db)[:3] == (identity.action, None, identity.setrow)
        disposition.assert_called_once()
    else:
        with pytest.raises(ConsistencyError, match="活动或RESULTS身份"):
            finish._identity(request, db)
        disposition.assert_not_called()
    db.execute.assert_not_called()


def test_event_inputs_keep_empty_pages_and_use_batch_facts_without_member_point_reads(monkeypatch):
    from camctl.capture.segment_models import SegmentSetRef
    from camctl.capture.segment_storage import SegmentOutputCandidate
    from camctl.contracts.pages import Page
    from camctl.outputs.catalog import OutputKind

    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    reader=SimpleNamespace(connection=db)
    request=FinishSegmentRecording(SegmentSetRef(1,8,9,1),3,4,5)
    port(monkeypatch,finish.storage,"_set",return_value={"publication_kind":int(enum_for("segment_sets.publication_kind").RESULT)})
    rows=port(monkeypatch,finish,"row_facts",return_value={})
    file={"original_name":"video.osv","media_type":"video/x-osv"}
    item=finish.storage.SavedSegmentCandidate(SegmentOutputCandidate(1,11,OutputKind.ORIGINAL),file)
    def pages(ref,cursor,batch,owned):
        assert ref==request.ref and batch==128 and owned is reader
        return Page((),"next") if cursor is None else Page((item,),None)
    candidates=port(monkeypatch,finish.storage,"read_segment_finish_candidates",side_effect=pages)
    scope=port(monkeypatch,finish.storage,"read_segment_finish_scope",side_effect=[Page((),"next"),Page((({"id":1,"member_id":1},11),),None)])
    get_candidates,get_scopes=finish._finish_event_inputs(request,reader,128)
    assert tuple(get_candidates())==((item.candidate,file),)
    assert tuple(get_scopes())==((),(({"id":1,"member_id":1},11),))
    assert candidates.call_count==scope.call_count==2
    rows.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.parametrize("native",[False,True])
def test_event_inputs_keep_canceled_video_candidate_partition(monkeypatch,native):
    from camctl.devices.tasks import CaptureResultPolicy
    from camctl.contracts.pages import Page

    db=create_autospec(sqlite3.Connection,instance=True,spec_set=True)
    request=FinishSegmentRecording(SegmentResultRef(1,2,1),3,4,5)
    port(monkeypatch,finish.storage,"_set",return_value={"publication_kind":int(enum_for("segment_sets.publication_kind").CANCELED)})
    port(monkeypatch,finish,"row_facts",return_value={"type":int(enum_for("actions.type").CAMERA_TIMELAPSE),
         "execution_spec_json":{"result_policy":CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value if native
         else CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value}})
    candidates=port(monkeypatch,finish.storage,"read_segment_finish_candidates",return_value=Page((),None))
    get_candidates,get_scopes=finish._finish_event_inputs(request,SimpleNamespace(connection=db),128)
    assert tuple(get_candidates())==tuple(get_scopes())==()
    assert candidates.call_count==int(native)
