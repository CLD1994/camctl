"""分别解释最后实际扫描与跨轮保留文件；不把两者合并为当前集合。"""

from dataclasses import replace
from types import SimpleNamespace

from camctl.capture.media import RecordingFailure
from camctl.capture.segment_models import (
    CompletionFacts,
    CurrentSegmentResult,
    DefaultScopeState,
    EvidenceState,
    ResultCheckEndReason,
    SegmentAccumulator,
    SegmentAssessmentReason,
    SegmentAssessmentStatus,
    CompletionBasis,
)
from camctl.capture.segment_errors import SegmentErrorHeader
from camctl.capture.segments import (
    advance_segment_assessment,
    finish_segment_assessment,
    iter_segment_gaps,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.devices.capture_timing import CaptureTiming
from camctl.persistence.transaction import row_facts

from .segment_result_sets import _one


def read_current_member_page(db, setrow, cursor, batch_size):
    """读取一个冻结成员批次，并保留原当前扫描的逐文件资格规则。"""
    from .segments import read_segment_members
    from .segment_result_sets import _current_input
    from .result_pages import _at
    from camctl.capture.segment_storage import SegmentResultRef
    from camctl.contracts.pages import Page

    ref = SegmentResultRef(setrow["action_id"], setrow["id"], setrow["version"])
    attempt = row_facts(db, "operation_attempts", setrow["current_attempt_id"])
    final, _ = _at(db, setrow["current_final_page_event_id"])
    boundary = HistoryBoundary(setrow["source_txn_id"], setrow["source_event_id"])
    page = read_segment_members(ref, cursor, batch_size, SimpleNamespace(connection=db))
    members = []
    for stored in page.items:
        actual = _one(
            db,
            "SELECT MAX(page_event_id) FROM result_page_files"
            " WHERE attempt_id=? AND file_id=? AND created_event_id<=?",
            (attempt["id"], stored.file_id, boundary.last_event_id),
        )[0]
        if actual is None:
            continue
        saved, _ = _at(db, actual)
        item = _current_input(
            db, saved.ref, stored.file_id, attempt, final.ref, boundary
        )
        entry = next(
            entry
            for entry in saved.page.entries
            if entry.identity == stored.member.identity
        )
        members.append(replace(
            stored.member,
            role=item.role,
            ownership=item.ownership,
            completion=item.completion,
            size_bytes=entry.size_bytes,
            pairing_identity=item.pairing_identity,
            pairing_state=item.pairing_state,
        ))
    return Page(tuple(members), page.next_cursor)


def current_members(db, setrow):
    """按已保存设备键逐成员核当前页；旧完成事实不能补当前页。"""
    cursor = None
    while True:
        page = read_current_member_page(db, setrow, cursor, 128)
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def current_assessment(db, setrow):
    if setrow["current_attempt_id"] is None:
        return None
    from .segment_starts import segment_start_from_set
    from .segment_result_sets import read_current_segment_ref
    from camctl.capture.segment_storage import SegmentResultRef

    reader = SimpleNamespace(connection=db)
    ref = read_current_segment_ref(
        SegmentResultRef(setrow["action_id"], setrow["id"], setrow["version"]), reader
    )
    start = segment_start_from_set(db, setrow)
    activity = row_facts(db, "device_activities", setrow["activity_id"])
    action = row_facts(db, "actions", setrow["action_id"])
    elapsed = activity["control_elapsed_ns"]
    timing = CaptureTiming(
        setrow["target_duration_ms"],
        action["execution_spec_json"].get("startup_margin_ms", 0),
    )
    control = (
        elapsed is not None
        and timing.recording_lower_bound_ns(elapsed) >= timing.duration_ms * 1_000_000
    )
    stop = (
        _one(
            db,
            "SELECT 1 FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
            " WHERE r.action_id=? AND r.kind=? AND t.effect_state=? AND t.result_event_id<=? LIMIT 1",
            (
                action["id"],
                int(enum_for("operation_runs.kind").STOP),
                int(enum_for("operation_attempts.effect_state").CONFIRMED),
                setrow["source_event_id"],
            ),
        )
        is not None
    )
    from .result_sources import load_result_source, ResultSourceKind
    from .result_pages import _at
    from camctl.devices.recording_completion import completed_file_wait_source

    final, _ = _at(db, setrow["current_final_page_event_id"])
    source = load_result_source(db, final.ref.ticket)
    wait_ms = action["execution_spec_json"].get("file_completion_wait_ms")
    waited = wait_ms is None
    if (
        wait_ms is not None
        and source.kind is ResultSourceKind.STOP
        and source.context is not None
    ):
        data = (
            None
            if final.page.outcome.settlement is None
            else final.page.outcome.settlement.evidence.data
        )
        prior = None
        page_id = (
            None if data is None else data.get("file_completion_source_page_event_id")
        )
        if page_id is not None:
            page, _ = _at(db, page_id)
            if (
                page_id <= final.ref.event_id
                and page.ref.ticket.run_id == final.ref.ticket.run_id
            ):
                prior = {
                    "event_id": page_id,
                    "activity_id": str(activity["id"]),
                    "data": None
                    if page.page.outcome.settlement is None
                    else page.page.outcome.settlement.evidence.data,
                }
        waited = (
            completed_file_wait_source(
                data,
                page_event_id=final.ref.event_id,
                activity_id=str(activity["id"]),
                stop_result_event_id=source.context["stop_result_event_id"],
                required_wait_ms=wait_ms,
                source_page=prior,
            )
            is not None
        )
    # 每个文件的严格完成证明另由 current_members 核实，空集合也核实际等待。
    confirmed = EvidenceState.CONFIRMED if stop else EvidenceState.UNKNOWN
    facts = CompletionFacts(
        confirmed,
        EvidenceState.CONFIRMED if waited else EvidenceState.UNKNOWN,
        EvidenceState.CONFIRMED,
        EvidenceState.CONFIRMED if start.key else EvidenceState.UNKNOWN,
        start.key,
        EvidenceState.CONFIRMED if control else EvidenceState.UNKNOWN,
        EvidenceState.UNKNOWN,
        True,
        False,
        start.observation,
    )
    state = SegmentAccumulator(ref, setrow["target_duration_ms"], start.key)
    for member in current_members(db, setrow):
        state = advance_segment_assessment(state, member)
    return finish_segment_assessment(state, facts)


def classify_action6_current_set(db, action, ref, setrow):
    """同步仓储独立形成决定，按原冻结页向纯规则提供成员。"""
    from camctl.capture.action6_completion import classify_action6_members
    from .segments import read_segment_members

    def members():
        cursor = None
        while True:
            page = read_segment_members(ref, cursor, 128, SimpleNamespace(connection=db))
            yield from (stored.member for stored in page.items)
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    return classify_action6_members(
        action, ref, members(),
        complete=setrow["source_kind"] == int(enum_for("segment_sets.source_kind").COMPLETE_SCAN)
        and setrow["publication_kind"] == int(enum_for("segment_sets.publication_kind").RESULT),
    )


def current_decision(db, setrow):
    """返回当前结论及其有限错误；保留全集不参与当前成功判定。"""
    latest = _one(
        db,
        "SELECT id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
        (setrow["run_id"],),
    )
    attempt = None if latest is None else row_facts(db, "operation_attempts", latest[0])
    if (
        attempt is not None
        and attempt["status"] == int(enum_for("operation_attempts.status").FAILED)
        and attempt["error_json"] is not None
    ):
        return (
            ResultCheckEndReason.EXPLICIT_RESULT_ERROR,
            CurrentSegmentResult.UNMET,
            RecordingFailure(
                "capture_failed",
                {"activity_id": str(setrow["activity_id"]), "reason": "device_failed"},
            ),
        )
    action = row_facts(db, "actions", setrow["action_id"])
    from camctl.devices.tasks import CaptureResultPolicy
    if (action["execution_spec_json"].get("result_policy")
            == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value):
        # 未完成 Action6 RESULTS 不能送入 OSMO 的 OSV 下界分类器；完整
        # 原扫描则按 Action6 的实际产品与视频顺序分类，供有限结论复核。
        from camctl.capture.segment_models import (
            SegmentDecision,
            SegmentDecisionStatus,
        )
        from camctl.capture.segment_storage import SegmentResultRef

        if setrow["source_kind"] == int(
            enum_for("segment_sets.source_kind").COMPLETE_SCAN
        ):
            from .segment_result_sets import read_current_segment_ref
            from camctl.capture.action6_completion import (
                action6_decision_failure,
            )

            result_ref = SegmentResultRef(
                setrow["action_id"], setrow["id"], setrow["version"]
            )
            reader = SimpleNamespace(connection=db)
            ref = read_current_segment_ref(result_ref, reader)
            if ref is None:
                raise ConsistencyError("Action6 完整扫描缺少当前冻结结果引用")
            from camctl.capture.segment_storage import assessment_from_document

            stored = (
                None
                if setrow["assessment_json"] is None
                else assessment_from_document(setrow["assessment_json"])
            )
            decision = stored if isinstance(stored, SegmentDecision) else None
            if decision is None:
                facts = classify_action6_current_set(db, action, ref, setrow)
                decision = facts.decision
            status = decision.status
            result = (
                CurrentSegmentResult.SUFFICIENT
                if status is SegmentDecisionStatus.SUCCEEDED
                else CurrentSegmentResult.UNMET
                if status is SegmentDecisionStatus.FAILED
                else CurrentSegmentResult.UNCONFIRMED
            )
            return (
                ResultCheckEndReason.SEGMENTS_SUFFICIENT,
                result,
                action6_decision_failure(setrow["activity_id"], decision),
            )
        return (
            ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED,
            CurrentSegmentResult.UNCONFIRMED,
            None,
        )
    assessment = current_assessment(db, setrow)
    if assessment is None:
        return (
            ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED,
            CurrentSegmentResult.NONE,
            None,
        )
    if assessment.reason is SegmentAssessmentReason.NO_NONZERO_ORIGINAL:
        present = (
            _one(
                db,
                "SELECT 1 FROM result_page_files WHERE attempt_id=? LIMIT 1",
                (setrow["current_attempt_id"],),
            )
            is not None
        )
        return (
            ResultCheckEndReason.ORIGINAL_ABSENT,
            CurrentSegmentResult.UNMET,
            RecordingFailure(
                "capture_failed",
                {
                    "activity_id": str(setrow["activity_id"]),
                    "reason": "invalid_outputs" if present else "no_outputs",
                },
            ),
        )
    if assessment.reason is SegmentAssessmentReason.GAP:
        from .segments import _eligible_original, read_segment_members
        from camctl.capture.segment_storage import SegmentResultRef

        ref = SegmentResultRef(setrow["action_id"], setrow["id"], setrow["version"])
        count = 0
        cursor = None
        while True:
            page = read_segment_members(
                ref, cursor, 128, SimpleNamespace(connection=db)
            )
            count += sum(_eligible_original(stored.member) for stored in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        header = SegmentErrorHeader(
            setrow["action_id"],
            assessment.gap_count,
            DefaultScopeState.UNKNOWN,
            None,
            count,
            assessment.first_gap,
        )
        return (
            ResultCheckEndReason.GAP_CONFIRMED,
            CurrentSegmentResult.UNMET,
            RecordingFailure(
                "capture_segment_gap", header.details("capture_segment_gap")
            ),
        )
    if (
        setrow["source_kind"] == int(enum_for("segment_sets.source_kind").COMPLETE_SCAN)
        and assessment.status is SegmentAssessmentStatus.SUCCEEDED
    ):
        return (
            ResultCheckEndReason.CONTROL_SUFFICIENT
            if assessment.basis is CompletionBasis.CONTROL
            else ResultCheckEndReason.SEGMENTS_SUFFICIENT,
            CurrentSegmentResult.SUFFICIENT,
            None,
        )
    return (
        ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED,
        CurrentSegmentResult.SUFFICIENT
        if assessment.status is SegmentAssessmentStatus.SUCCEEDED
        else CurrentSegmentResult.UNCONFIRMED,
        None,
    )


def current_gaps(db, setrow):
    assessment = current_assessment(db, setrow)
    if assessment is None or assessment.gap_count is None:
        return
    yield from iter_segment_gaps(
        current_members(db, setrow),
        last_nonzero_cursor=assessment.last_nonzero_cursor,
        last_owned_cursor=assessment.last_owned_cursor,
        start_key=assessment.confirmed_start_key,
    )
