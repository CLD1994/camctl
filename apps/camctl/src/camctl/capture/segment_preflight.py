"""固定结果前的有界分类，仅决定是否继续原有限核实。"""

from camctl.capture.segment_models import CompletionFacts, EvidenceState, SegmentAccumulator, SegmentSetRef
from camctl.capture.segment_storage import SegmentSourceKind
from camctl.capture.segments import advance_segment_assessment, finish_segment_assessment
from camctl.persistence.repositories.segment_listing import ordered_listing_members
from camctl.persistence.transaction import row_facts


def assess_current_listing(runtime, action, listing, control, overrun):
    """原页逐文件存入临时磁盘排序；不固定集合，也不收集全部文件。"""
    from camctl.persistence.repositories.segment_result_sets import _boundary
    from camctl.persistence.repositories.segment_starts import segment_start_from_set
    from camctl.capture import handlers as common
    from camctl.persistence.repositories.segments import SegmentRepository

    prepared = SegmentRepository().prepare_result_set(
        action["id"], runtime.wall_us(), runtime.owned
    )
    if prepared.source_kind is not SegmentSourceKind.COMPLETE_SCAN:
        return None
    db = runtime.owned.connection
    boundary = _boundary(db)
    attempt = row_facts(
        db,
        "operation_attempts",
        db.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (listing.ticket.run_id, listing.ticket.attempt_id),
        ).fetchone()[0],
    )
    ref = SegmentSetRef(
        action["id"], attempt["id"], listing.entries.last_ref.event_id, 1
    )
    fake = dict(
        id=0,
        action_id=action["id"],
        activity_id=prepared.activity_id,
        run_id=prepared.run_id,
        current_attempt_id=attempt["id"],
        source_txn_id=boundary.txn_id,
        source_event_id=boundary.last_event_id,
    )
    start = segment_start_from_set(db, fake)
    stop = common._stop_confirmed_at(runtime, action["id"]) is not None
    completion = common._record_file_completion_context(runtime, action["id"])
    waited = stop and (
        action["execution_spec_json"].get("file_completion_wait_ms") is None
        or completion is not None
        and completion["prior_completion"] is not None
    )
    facts = CompletionFacts(
        EvidenceState.CONFIRMED if stop else EvidenceState.UNKNOWN,
        EvidenceState.CONFIRMED if waited else EvidenceState.UNKNOWN,
        EvidenceState.CONFIRMED,
        EvidenceState.CONFIRMED if start.key else EvidenceState.UNKNOWN,
        start.key,
        control,
        overrun,
        False,
        False,
        start.observation,
    )
    state = SegmentAccumulator(
        ref, action["execution_spec_json"]["target_duration_ms"], start.key
    )
    with ordered_listing_members(runtime.owned, listing) as members:
        for member in members():
            state = advance_segment_assessment(state, member)
    return finish_segment_assessment(state, facts)
