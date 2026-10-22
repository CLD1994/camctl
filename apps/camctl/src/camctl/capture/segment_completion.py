"""按冻结任务策略推进分段结果，复用控制与分页，不进入单原片媒体链。"""

from dataclasses import dataclass, replace

from camctl.capture.files import (
    FileCompletionSave,
    FilePresenceSave,
    OwnershipFromResultPageSave,
    PairResultFilesSave,
)
from camctl.capture.media import RecordingFailure
from camctl.capture.models import ActivityReleaseSave
from camctl.capture.segment_errors import segment_error_header
from camctl.capture.segment_finish import FinishSegmentRecording
from camctl.capture.segment_models import (
    CompletionFacts,
    DefaultScopeState,
    EvidenceState,
    SegmentAccumulator,
    SegmentAssessmentReason,
    SegmentAssessmentStatus,
    SegmentSourceKind,
)
from camctl.capture.segments import (
    advance_segment_assessment,
    finish_segment_assessment,
    iter_segment_gaps,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.capture_timing import CaptureTiming
from camctl.devices.recording_completion import is_completed_file_wait
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository
from camctl.persistence.repositories.result_sources import ResultSourceKind
from camctl.persistence.transaction import row_facts


@dataclass(frozen=True)
class PendingSegmentWrite:
    repository: object
    method: str
    args: tuple
    key: str


def resume_segment_writes(owned, pending_writes, action_id=None):
    if pending_writes and owned.connection.in_transaction:
        owned.connection.rollback()
    for identity in tuple(pending_writes):
        if action_id is not None and identity != action_id:
            continue
        pending = pending_writes[identity]
        receipt = getattr(pending.repository, pending.method)(
            *pending.args, pending.key, owned
        )
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            if receipt.kind is DbOutcomeKind.ROLLED_BACK:
                del pending_writes[identity]
            raise ConsistencyError(f"原分段保存尚未可靠完成: {receipt.error}")
        del pending_writes[identity]


def _save(runtime, action_id, repository, method, *args):
    if action_id in runtime.pending_segment_writes:
        raise ConsistencyError("须先核实原分段申请，不能替换未决保存")
    pending = PendingSegmentWrite(repository, method, args, new_operation_key())
    runtime.pending_segment_writes[action_id] = pending
    receipt = getattr(repository, method)(*args, pending.key, runtime.owned)
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        if receipt.kind is DbOutcomeKind.ROLLED_BACK:
            del runtime.pending_segment_writes[action_id]
        raise ConsistencyError(f"分段保存尚未可靠完成: {receipt.error}")
    del runtime.pending_segment_writes[action_id]
    return receipt.value


def release_segment_occupancy(runtime, action_id):
    """沿原申请交接释放结果；条件不足时由既有仓储保持占用。"""
    _save(
        runtime,
        action_id,
        runtime.capture,
        "release_occupancy",
        ActivityReleaseSave(action_id, runtime.wall_us()),
    )


def register_segment_observations(runtime, action_id, listing):
    """先保存全部原始 OSV/LRF 归属，再独立保存完成及跨页配对。"""
    from camctl.capture.result_scans import SavedResultEntries

    if not isinstance(listing.entries, SavedResultEntries):
        if (listing.entries == () and listing.already_saved
                and runtime.capture.read_last_result_page(listing.ticket, runtime.owned) is None):
            return
        raise ConsistencyError("分段策略要求可靠原始结果页")
    source = runtime.capture.read_result_source(listing.ticket, runtime.owned)
    context = source.context
    activity_id = int(listing.ticket.target_id)
    activity = row_facts(runtime.owned.connection, "device_activities", activity_id)
    for saved in listing.entries.pages():
        ids = dict(saved.file_ids)
        for entry in saved.page.entries:
            if entry.identity not in ids:
                continue
            file_id = ids[entry.identity]
            file = row_facts(runtime.owned.connection, "device_files", file_id)
            if file["presence_state"] != int(
                enum_for("device_files.presence_state").PRESENT
            ):
                _save(
                    runtime,
                    action_id,
                    runtime.capture,
                    "save_file_presence",
                    FilePresenceSave(
                        file_id,
                        int(enum_for("device_files.presence_state").PRESENT),
                        saved.occurred_at,
                    ),
                )
            _save(
                runtime,
                action_id,
                runtime.capture,
                "save_ownership_from_result_page",
                OwnershipFromResultPageSave(saved.ref, file_id, saved.occurred_at),
            )
            if not entry.complete or entry.size_bytes is None:
                continue
            if source.kind is ResultSourceKind.STOP:
                data = (
                    None
                    if saved.page.outcome.settlement is None
                    else saved.page.outcome.settlement.evidence.data
                )
                completion = None if data is None else data.get("file_completion")
                if context is None or not is_completed_file_wait(
                    completion,
                    activity_id=context["activity_id"],
                    stop_result_event_id=context["stop_result_event_id"],
                    required_wait_ms=context["file_completion_wait_ms"],
                ):
                    continue
                request = FileCompletionSave(
                    file_id,
                    3,
                    saved.occurred_at,
                    basis=int(
                        enum_for(
                            "device_files.completion_evidence_json.basis"
                        ).STOP_RETURN_AND_WAIT
                    ),
                    observation=entry.evidence,
                    size_bytes=entry.size_bytes,
                    activity_id=activity_id,
                    result_page_event_id=saved.ref.event_id,
                    stop_result_event_id=context["stop_result_event_id"],
                )
            elif source.kind is ResultSourceKind.NATURAL and context is not None:
                request = FileCompletionSave(
                    file_id,
                    3,
                    saved.occurred_at,
                    basis=int(
                        enum_for(
                            "device_files.completion_evidence_json.basis"
                        ).TIME_AND_OUTPUTS
                    ),
                    observation=entry.evidence,
                    size_bytes=entry.size_bytes,
                    activity_id=activity_id,
                    result_page_event_id=saved.ref.event_id,
                    start_result_event_id=context["start_result_event_id"],
                    wait_completed_event_id=activity["wait_completed_event_id"],
                )
            else:
                continue
            _save(runtime, action_id, runtime.capture, "save_file_completion", request)
    if listing.scan_complete and listing.set_finalized:
        from camctl.persistence.repositories.segment_result_sets import _boundary
        from camctl.persistence.repositories.result_file_pairing import (
            verify_pairing_fact,
        )

        for saved in listing.entries.pages():
            for identity, file_id in saved.file_ids:
                if identity.rpartition(".")[2].lower() != "lrf":
                    continue
                existing = runtime.owned.connection.execute(
                    "SELECT p.id FROM device_file_pairing_facts p"
                    " JOIN operation_attempts a ON a.id=p.attempt_id"
                    " WHERE p.preview_file_id=? AND a.run_id=? AND a.attempt_no=?"
                    " AND p.final_page_event_id=?",
                    (
                        file_id,
                        listing.ticket.run_id,
                        listing.ticket.attempt_id,
                        listing.entries.last_ref.event_id,
                    ),
                ).fetchone()
                if existing is not None:
                    verify_pairing_fact(
                        runtime.owned.connection,
                        existing[0],
                        file_id,
                        _boundary(runtime.owned.connection),
                    )
                    continue
                _save(
                    runtime,
                    action_id,
                    runtime.capture,
                    "save_result_file_pairing",
                    PairResultFilesSave(
                        file_id,
                        listing.entries.last_ref,
                        _boundary(runtime.owned.connection),
                        runtime.wall_us(),
                    ),
                )


def _members(repository, ref, runtime):
    cursor = None
    while True:
        page = repository.read_members(ref, cursor, 128, runtime.owned)
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def _control_facts(runtime, action, activity):
    elapsed = activity["control_elapsed_ns"]
    if elapsed is None:
        return EvidenceState.UNKNOWN, EvidenceState.UNKNOWN
    timing = CaptureTiming(
        action["execution_spec_json"]["target_duration_ms"],
        action["execution_spec_json"].get("startup_margin_ms", 0),
    )
    lower = timing.recording_lower_bound_ns(elapsed)
    complete = lower >= timing.duration_ms * 1_000_000
    # 正常控制的轻微余量沿既有异常多录门槛处理。
    overrun = lower > (
        timing.duration_ms * 1_000_000 + runtime.repair_margin_s * 1_000_000_000
    )
    return (
        EvidenceState.CONFIRMED if complete else EvidenceState.UNKNOWN,
        EvidenceState.CONFIRMED if overrun else EvidenceState.UNKNOWN,
    )


def _prepare_members(runtime, repository, ref):
    row = repository.read_result_set(ref, runtime.owned)
    if row["state"] != int(enum_for("segment_sets.state").COLLECTING):
        return
    from camctl.capture.segment_storage import ResultMemberInputCursor

    previous = runtime.owned.connection.execute(
        "SELECT MAX(file_id) FROM segment_members WHERE set_id=?", (ref.set_id,)
    ).fetchone()[0]
    cursor = None if previous is None else ResultMemberInputCursor(ref, previous)
    while True:
        page = repository.read_result_member_inputs(ref, cursor, 128, runtime.owned)
        if page.items:
            _save(
                runtime,
                ref.action_id,
                repository,
                "append_result_members",
                ref,
                page.items,
                runtime.wall_us(),
            )
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    _save(
        runtime, ref.action_id, repository, "fix_result_members", ref, runtime.wall_us()
    )


async def advance_segmented_recording(runtime, action, decision):
    """普通分段结果在同一发布事务登记全部产物、默认范围和动作结果。"""
    from camctl.capture import handlers as common

    action_id = action["id"]
    resume_segment_writes(runtime.owned, runtime.pending_segment_writes, action_id)
    repository = SegmentRepository()
    ref = repository.read_prepared_result_ref(action_id, runtime.owned)
    activity = row_facts(
        runtime.owned.connection,
        "device_activities",
        common._activity_id_of(runtime, action_id),
    )
    control, overrun = _control_facts(runtime, action, activity)
    from camctl.persistence.repositories.segment_current import current_decision

    if ref is None:
        listing = await common._listing_round(runtime, action_id)
        if listing.phase in (
            common.ListingPhase.IN_FLIGHT,
            common.ListingPhase.RETRY_WAIT,
        ):
            return
        if listing.phase is common.ListingPhase.EXHAUSTED:
            listing = common._saved_result_listing(runtime, action_id)
        register_segment_observations(runtime, action_id, listing)
        if runtime.action(action_id)["cancel_requested"]:
            common._finish_listing_result(runtime, listing, finalize_output_set=True)
            release_segment_occupancy(runtime, action_id)
            finish_segmented_cancellation(runtime, action_id)
            return
        used = row_facts(
            runtime.owned.connection, "operation_runs", listing.ticket.run_id
        )["attempts_used"]
        explicit_error = (
            listing.outcome is not None and listing.outcome.error is not None
        )
        if (
            not explicit_error
            and used < runtime.check_config.max_attempts
            and not listing.already_saved
        ):
            from camctl.capture.segment_preflight import assess_current_listing

            assessment = assess_current_listing(
                runtime, action, listing, control, overrun
            )
            if assessment is None or assessment.status not in (
                SegmentAssessmentStatus.SUCCEEDED,
                SegmentAssessmentStatus.FAILED,
            ):
                common._finish_listing_result(runtime, listing, retry_wait=True)
                return
        common._finish_listing_result(runtime, listing, finalize_output_set=True)
        request = repository.prepare_result_set(
            action_id, runtime.wall_us(), runtime.owned
        )
        ref = _save(runtime, action_id, repository, "create_result_set", request)
    else:
        # 原组已开始固定后重入，目录结束仍沿原末页与原保存责任核实。
        listing = common._saved_result_listing(runtime, action_id)
        common._finish_listing_result(runtime, listing, finalize_output_set=True)
    release_segment_occupancy(runtime, action_id)
    _prepare_members(runtime, repository, ref)
    scan_ref = repository.read_current_segment_ref(ref, runtime.owned)
    setrow = repository.read_result_set(ref, runtime.owned)
    from camctl.capture.segment_storage import ResultCheckConclusionSave

    reason, _, current_failure = current_decision(runtime.owned.connection, setrow)
    found = runtime.owned.connection.execute(
        "SELECT id FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
    ).fetchone()
    if found is None:
        (anchor,) = runtime.owned.connection.execute(
            "SELECT result_event_id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
            (setrow["run_id"],),
        ).fetchone()
        _save(
            runtime,
            action_id,
            repository,
            "conclude_result_check",
            ResultCheckConclusionSave(
                ref,
                reason,
                anchor,
                None,
                runtime.check_config,
                None,
                None,
                runtime.wall_us(),
            ),
        )
    if setrow["source_kind"] != int(
        enum_for("segment_sets.source_kind")[SegmentSourceKind.COMPLETE_SCAN.name]
    ):
        from camctl.persistence.repositories.segment_current import current_gaps

        saved_count = runtime.owned.connection.execute(
            "SELECT COUNT(*) FROM segment_gaps WHERE set_id=?", (ref.set_id,)
        ).fetchone()[0]
        batch = []
        if setrow["state"] == int(enum_for("segment_sets.state").MEMBERS_FIXED):
            for number, gap in enumerate(
                current_gaps(runtime.owned.connection, setrow)
            ):
                if number >= saved_count:
                    batch.append(gap)
                    if len(batch) == 128:
                        _save(
                            runtime,
                            action_id,
                            repository,
                            "append_gaps",
                            ref,
                            tuple(batch),
                            runtime.wall_us(),
                        )
                        batch.clear()
            if batch:
                _save(
                    runtime,
                    action_id,
                    repository,
                    "append_gaps",
                    ref,
                    tuple(batch),
                    runtime.wall_us(),
                )
        failure = current_failure or RecordingFailure(
            "capture_result_unconfirmed",
            {"activity_id": str(activity["id"]), "reason": "outputs_unknown"},
        )
        if setrow["state"] == int(enum_for("segment_sets.state").MEMBERS_FIXED):
            _save(
                runtime, action_id, repository, "decide_result", ref, runtime.wall_us()
            )
        _save(
            runtime,
            action_id,
            runtime.capture,
            "finish_segment_recording",
            FinishSegmentRecording(
                ref, activity["id"], setrow["run_id"], runtime.wall_us(), failure
            ),
        )
        return
    if setrow["state"] == int(enum_for("segment_sets.state").MEMBERS_FIXED):
        start = repository.read_start(ref, runtime.owned)
        stop = common._stop_confirmed_at(runtime, action_id) is not None
        completion = common._record_file_completion_context(runtime, action_id)
        wait = stop and (
            action["execution_spec_json"].get("file_completion_wait_ms") is None
            or completion is not None
            and completion["prior_completion"] is not None
        )
        facts = CompletionFacts(
            EvidenceState.CONFIRMED if stop else EvidenceState.UNKNOWN,
            EvidenceState.CONFIRMED if wait else EvidenceState.UNKNOWN,
            EvidenceState.CONFIRMED,
            EvidenceState.CONFIRMED if start.key is not None else EvidenceState.UNKNOWN,
            start.key,
            control,
            overrun,
            True,
            False,
            start.observation,
        )
        state = SegmentAccumulator(scan_ref, setrow["target_duration_ms"], start.key)
        for stored in _members(repository, scan_ref, runtime):
            state = advance_segment_assessment(state, stored.member)
        assessment = finish_segment_assessment(state, facts)
        saved_count = runtime.owned.connection.execute(
            "SELECT COUNT(*) FROM segment_gaps WHERE set_id=?", (ref.set_id,)
        ).fetchone()[0]
        batch = []
        if assessment.gap_count is not None:
            for number, gap in enumerate(
                iter_segment_gaps(
                    (
                        stored.member
                        for stored in _members(repository, scan_ref, runtime)
                    ),
                    last_nonzero_cursor=assessment.last_nonzero_cursor,
                    last_owned_cursor=assessment.last_owned_cursor,
                    start_key=start.key,
                )
            ):
                if number < saved_count:
                    continue
                batch.append(gap)
                if len(batch) == 128:
                    _save(
                        runtime,
                        action_id,
                        repository,
                        "append_gaps",
                        scan_ref,
                        tuple(batch),
                        runtime.wall_us(),
                    )
                    batch.clear()
        if batch:
            _save(
                runtime,
                action_id,
                repository,
                "append_gaps",
                scan_ref,
                tuple(batch),
                runtime.wall_us(),
            )
        _save(
            runtime,
            action_id,
            repository,
            "save_assessment",
            scan_ref,
            facts,
            assessment,
            runtime.wall_us(),
        )
    assessment = repository.read_assessment(scan_ref, runtime.owned)
    if assessment.default_scope.state is DefaultScopeState.NONEMPTY:
        count = runtime.owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members WHERE set_id=?", (ref.set_id,)
        ).fetchone()[0]
        selected = 0
        batch = []
        from camctl.persistence.repositories.segments import _eligible_original

        for stored in _members(repository, scan_ref, runtime):
            member = stored.member
            if (
                not _eligible_original(member)
                or member.cursor is None
                or not assessment.default_scope.first_cursor
                <= member.cursor
                <= assessment.default_scope.last_cursor
            ):
                continue
            if selected >= count:
                batch.append(stored.member_id)
                if len(batch) == 128:
                    _save(
                        runtime,
                        action_id,
                        repository,
                        "append_scope",
                        scan_ref,
                        tuple(batch),
                        runtime.wall_us(),
                    )
                    batch.clear()
            selected += 1
        if batch:
            _save(
                runtime,
                action_id,
                repository,
                "append_scope",
                scan_ref,
                tuple(batch),
                runtime.wall_us(),
            )
    failure = None
    if assessment.status is not SegmentAssessmentStatus.SUCCEEDED:
        if assessment.reason is SegmentAssessmentReason.GAP:
            from camctl.persistence.repositories.segments import _eligible_original

            original_count = sum(
                _eligible_original(stored.member)
                for stored in _members(repository, scan_ref, runtime)
            )
            failure = RecordingFailure(
                "capture_segment_gap",
                segment_error_header(assessment, original_count).details(
                    "capture_segment_gap"
                ),
            )
        elif assessment.reason is SegmentAssessmentReason.NO_NONZERO_ORIGINAL:
            failure = RecordingFailure(
                "capture_failed",
                {
                    "activity_id": str(activity["id"]),
                    "reason": "no_outputs"
                    if setrow["member_count"] == 0
                    else "invalid_outputs",
                },
            )
        else:
            failure = RecordingFailure(
                "capture_result_unconfirmed",
                {
                    "activity_id": str(activity["id"]),
                    "reason": "completion_unknown"
                    if assessment.reason
                    in (
                        SegmentAssessmentReason.STOP_UNCONFIRMED,
                        SegmentAssessmentReason.COMPLETION_UNCONFIRMED,
                        SegmentAssessmentReason.LOWER_BOUND_INSUFFICIENT,
                    )
                    else "outputs_unknown",
                },
            )
    _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            scan_ref, activity["id"], setrow["run_id"], runtime.wall_us(), failure
        ),
    )


def finish_segmented_cancellation(runtime, action_id):
    """取消接手同一原观察边界，发布零产物的明确取消版本。"""
    from camctl.capture.segment_storage import ResultCheckConclusionSave
    from camctl.capture.segment_models import PublicationKind, ResultCheckEndReason
    from camctl.contracts.history_values import HistoryBoundary

    resume_segment_writes(runtime.owned, runtime.pending_segment_writes, action_id)
    db = runtime.owned.connection
    from camctl.operations.attempts import StaleRunFinish, RunOutcome

    if db.execute(
        "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.action_id=? AND t.status=1 LIMIT 1",
        (action_id,),
    ).fetchone():
        raise ConsistencyError("分段取消须先可靠结束原在途调用")
    stale = tuple(
        row[0]
        for row in db.execute(
            "SELECT responsibility_key FROM operation_runs"
            " WHERE action_id=? AND kind!=? AND status IN (1,2) ORDER BY id",
            (action_id, int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS)),
        )
    )
    if stale:
        _save(
            runtime,
            action_id,
            runtime.operations,
            "finish_stale_runs",
            StaleRunFinish(stale, RunOutcome.CANCELED, runtime.wall_us()),
        )
    repository = SegmentRepository()
    cancel = db.execute(
        "SELECT e.id FROM history_events e JOIN json_each(e.body_json,'$.rows') r "
        "WHERE json_extract(r.value,'$.table')='actions' AND json_extract(r.value,'$.id')=? "
        "AND json_extract(r.value,'$.before.values.cancel_requested')=0 "
        "AND json_extract(r.value,'$.after.values.cancel_requested')=1 ORDER BY e.id DESC LIMIT 1",
        (action_id,),
    ).fetchone()
    if cancel is None:
        raise ConsistencyError("分段取消缺少实际取消请求")
    previous = repository.read_prepared_result_ref(action_id, runtime.owned)
    predecessor = None if previous is None else previous.set_id
    ref = repository.find_canceled_result_ref(
        action_id, predecessor, cancel[0], runtime.owned
    )
    if ref is None:
        request = repository.prepare_result_set(
            action_id, runtime.wall_us(), runtime.owned
        )
        if previous is not None:
            row = repository.read_result_set(previous, runtime.owned)
            from camctl.persistence.repositories.result_pages import _at

            request = replace(
                request,
                source_boundary=HistoryBoundary(
                    row["source_txn_id"], row["source_event_id"]
                ),
                source_kind=SegmentSourceKind[
                    enum_for("segment_sets.source_kind")(row["source_kind"]).name
                ],
                current_scan=(
                    None
                    if row["current_attempt_id"] is None
                    else _at(db, row["current_final_page_event_id"])[0].ref
                ),
            )
        request = replace(
            request,
            publication_kind=PublicationKind.CANCELED,
            predecessor_set_id=predecessor,
            cancel_event_id=cancel[0],
        )
        ref = _save(runtime, action_id, repository, "create_result_set", request)
    _prepare_members(runtime, repository, ref)
    row = repository.read_result_set(ref, runtime.owned)
    conclusion = db.execute(
        "SELECT id FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
    ).fetchone()
    if conclusion is None:
        run = (
            None
            if row["run_id"] is None
            else row_facts(db, "operation_runs", row["run_id"])
        )
        prior_end = None
        if run is not None and run["status"] not in (1, 2):
            (prior_end,) = db.execute(
                "SELECT e.id FROM history_events e JOIN json_each(e.body_json,'$.rows') r "
                "WHERE json_extract(r.value,'$.table')='operation_runs' AND json_extract(r.value,'$.id')=? "
                "AND json_extract(r.value,'$.before.values.status') IN (1,2) "
                "AND json_extract(r.value,'$.after.values.status') IN (3,4,5,6,7) ORDER BY e.id DESC LIMIT 1",
                (run["id"],),
            ).fetchone()
        prior = (
            None
            if predecessor is None
            else db.execute(
                "SELECT id FROM segment_check_conclusions WHERE set_id=?",
                (predecessor,),
            ).fetchone()
        )
        _save(
            runtime,
            action_id,
            repository,
            "conclude_result_check",
            ResultCheckConclusionSave(
                ref,
                ResultCheckEndReason.CANCELED_BEFORE_PUBLICATION,
                cancel[0],
                prior_end,
                runtime.check_config if run is not None and prior_end is None else None,
                None,
                None if prior is None else prior[0],
                runtime.wall_us(),
            ),
        )
    if row["state"] == int(enum_for("segment_sets.state").MEMBERS_FIXED):
        _save(runtime, action_id, repository, "decide_result", ref, runtime.wall_us())
    _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            ref, row["activity_id"], row["run_id"], runtime.wall_us()
        ),
    )


def finish_segmented_baseline_failure(runtime, action_id):
    """基准实际失败也发布明确的无 RESULTS 来源，不派发 START。"""
    from camctl.capture.segment_storage import ResultCheckConclusionSave
    from camctl.capture.segment_models import ResultCheckEndReason

    resume_segment_writes(runtime.owned, runtime.pending_segment_writes, action_id)
    db = runtime.owned.connection
    repository = SegmentRepository()
    ref = repository.read_prepared_result_ref(action_id, runtime.owned)
    if ref is None:
        ref = _save(
            runtime,
            action_id,
            repository,
            "create_result_set",
            repository.prepare_result_set(action_id, runtime.wall_us(), runtime.owned),
        )
    _prepare_members(runtime, repository, ref)
    row = repository.read_result_set(ref, runtime.owned)
    if (
        db.execute(
            "SELECT 1 FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
        ).fetchone()
        is None
    ):
        (anchor,) = db.execute(
            "SELECT result_event_id FROM baseline_read_calls WHERE action_id=? ORDER BY read_no DESC LIMIT 1",
            (action_id,),
        ).fetchone()
        _save(
            runtime,
            action_id,
            repository,
            "conclude_result_check",
            ResultCheckConclusionSave(
                ref,
                ResultCheckEndReason.NO_RESULT_WORK,
                anchor,
                None,
                None,
                None,
                None,
                runtime.wall_us(),
            ),
        )
    if row["state"] == int(enum_for("segment_sets.state").MEMBERS_FIXED):
        _save(runtime, action_id, repository, "decide_result", ref, runtime.wall_us())
    _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            ref,
            row["activity_id"],
            None,
            runtime.wall_us(),
            RecordingFailure(
                "capture_failed",
                {
                    "activity_id": str(row["activity_id"]),
                    "reason": "baseline_read_failed",
                },
            ),
        ),
    )


def finish_segmented_start_failure(runtime, action_id):
    """START 实际结束但没有 RESULTS 时，发布未知范围及原启动失败。"""
    from camctl.capture.segment_storage import ResultCheckConclusionSave
    from camctl.capture.segment_models import ResultCheckEndReason
    from camctl.persistence.repositories.segment_check_conclusions import (
        no_result_start_failure,
    )

    resume_segment_writes(runtime.owned, runtime.pending_segment_writes, action_id)
    if runtime.action(action_id)["status"] in (3, 4, 5, 6):
        return
    db = runtime.owned.connection
    repository = SegmentRepository()
    ref = repository.read_prepared_result_ref(action_id, runtime.owned)
    if ref is None:
        ref = _save(
            runtime,
            action_id,
            repository,
            "create_result_set",
            repository.prepare_result_set(action_id, runtime.wall_us(), runtime.owned),
        )
    _prepare_members(runtime, repository, ref)
    row = repository.read_result_set(ref, runtime.owned)
    (anchor,) = db.execute(
        "SELECT t.result_event_id FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.action_id=? AND r.kind=? ORDER BY t.attempt_no DESC LIMIT 1",
        (action_id, int(enum_for("operation_runs.kind").START)),
    ).fetchone()
    failure = no_result_start_failure(db, row, anchor)
    if (
        db.execute(
            "SELECT 1 FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
        ).fetchone()
        is None
    ):
        _save(
            runtime,
            action_id,
            repository,
            "conclude_result_check",
            ResultCheckConclusionSave(
                ref,
                ResultCheckEndReason.NO_RESULT_WORK,
                anchor,
                None,
                None,
                None,
                None,
                runtime.wall_us(),
            ),
        )
    if row["state"] == int(enum_for("segment_sets.state").MEMBERS_FIXED):
        _save(runtime, action_id, repository, "decide_result", ref, runtime.wall_us())
    _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            ref, row["activity_id"], None, runtime.wall_us(), failure
        ),
    )


def resume_closed_start(runtime, action):
    """冷恢复接手原 START 结束，不重新授予启动或更换已保存错误。"""
    if action["execution_spec_json"].get("result_policy") is None:
        return False
    db = runtime.owned.connection
    run = db.execute(
        "SELECT status FROM operation_runs WHERE action_id=? AND kind=?",
        (action["id"], int(enum_for("operation_runs.kind").START)),
    ).fetchone()
    results = db.execute(
        "SELECT id FROM operation_runs WHERE action_id=? AND kind=?",
        (action["id"], int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS)),
    ).fetchone()
    if run is None or run[0] not in (4, 6) or results is not None:
        return False
    if action["cancel_requested"]:
        finish_segmented_cancellation(runtime, action["id"])
    else:
        finish_segmented_start_failure(runtime, action["id"])
    return True


def finish_segmented_external_failure(runtime, action_id, reason, failure, anchor=None):
    """外部收场仅接手已保存的观察，按同一发布边界保留合格文件。"""
    from camctl.capture.segment_storage import ResultCheckConclusionSave
    from camctl.capture.segment_models import ResultCheckEndReason

    resume_segment_writes(runtime.owned, runtime.pending_segment_writes, action_id)
    db = runtime.owned.connection
    repository = SegmentRepository()
    ref = repository.read_prepared_result_ref(action_id, runtime.owned)
    if ref is None:
        ref = _save(
            runtime,
            action_id,
            repository,
            "create_result_set",
            repository.prepare_result_set(action_id, runtime.wall_us(), runtime.owned),
        )
    _prepare_members(runtime, repository, ref)
    row = repository.read_result_set(ref, runtime.owned)
    if (
        db.execute(
            "SELECT 1 FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
        ).fetchone()
        is None
    ):
        _save(
            runtime,
            action_id,
            repository,
            "conclude_result_check",
            ResultCheckConclusionSave(
                ref,
                reason,
                anchor,
                None,
                runtime.check_config if row["run_id"] is not None else None,
                failure if reason is ResultCheckEndReason.BINDING_UNAVAILABLE else None,
                None,
                runtime.wall_us(),
            ),
        )
    if row["state"] == int(enum_for("segment_sets.state").MEMBERS_FIXED):
        _save(runtime, action_id, repository, "decide_result", ref, runtime.wall_us())
    _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            ref, row["activity_id"], row["run_id"], runtime.wall_us(), failure
        ),
    )
