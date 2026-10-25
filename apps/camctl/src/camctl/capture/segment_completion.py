"""按冻结任务策略推进分段结果，复用控制与分页，不进入单原片媒体链。"""

from dataclasses import dataclass, field, replace

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
from camctl.persistence.models import DbOutcomeKind, db_outcome_for
from camctl.persistence.business import SharedSave, settle_required
from camctl.persistence.repositories.segments import SegmentRepository
from camctl.persistence.repositories.result_sources import ResultSourceKind
from camctl.persistence.transaction import row_facts
from camctl.logging_runtime.performance import measure_performance
from camctl.logging_runtime.transaction_timing import db_operation_context


@dataclass(frozen=True)
class PendingSegmentWrite:
    repository: object
    method: str
    args: tuple
    key: str
    save: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)


async def resume_segment_writes(database, pending_writes, action_id=None):
    return await settle_required(_resume_segment_writes(database, pending_writes, action_id))


async def _resume_segment_writes(database, pending_writes, action_id):
    for identity, pending in tuple(pending_writes.items()):
        if action_id is not None and identity != action_id:
            continue
        pending.save.retry_failed()
        with measure_performance("segment_save", action_id=identity,
                                 method=pending.method, resumed=True), db_operation_context(
                                     action_id=identity, method=pending.method, resumed=True):
            await pending.save.run(lambda: _write_segment_request(
                database, pending_writes, identity, pending))


async def _save(runtime, action_id, repository, method, *args):
    return await settle_required(_save_segment_request(runtime, action_id, repository, method, args))


async def _write_segment_request(database, pending_writes, action_id, pending):
    try:
        receipt = await database.write(
            lambda owned: getattr(pending.repository, pending.method)(*pending.args, pending.key, owned),
            key=pending.key, description=f'分段保存:{pending.method}')
    except BaseException as error:
        receipt = db_outcome_for(error)
        if receipt is not None and receipt.kind in (DbOutcomeKind.COMPLETED, DbOutcomeKind.ROLLED_BACK):
            if pending_writes.get(action_id) is pending:
                del pending_writes[action_id]
        raise
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        if receipt.kind is DbOutcomeKind.ROLLED_BACK and pending_writes.get(action_id) is pending:
            del pending_writes[action_id]
        raise ConsistencyError(f"原分段保存尚未可靠完成: {receipt.error}")
    if pending_writes.get(action_id) is pending:
        del pending_writes[action_id]
    return receipt.value


async def _save_segment_request(runtime, action_id, repository, method, args):
    if action_id in runtime.pending_segment_writes:
        raise ConsistencyError("须先核实原分段申请，不能替换未决保存")
    pending = PendingSegmentWrite(repository, method, args, new_operation_key())
    runtime.pending_segment_writes[action_id] = pending
    with measure_performance("segment_save", action_id=action_id,
                             method=method, resumed=False), db_operation_context(
                                 action_id=action_id, method=method, resumed=False):
        return await pending.save.run(lambda: _write_segment_request(
            runtime.database, runtime.pending_segment_writes, action_id, pending))


async def release_segment_occupancy(runtime, action_id):
    """沿原申请交接释放结果；条件不足时由既有仓储保持占用。"""
    await _save(
        runtime,
        action_id,
        runtime.capture,
        "release_occupancy",
        ActivityReleaseSave(action_id, runtime.wall_us()),
    )


async def register_segment_observations(runtime, action_id, listing):
    """先保存全部原始 OSV/LRF 归属，再独立保存完成及跨页配对。"""
    from camctl.capture.result_scans import SavedResultEntries

    if not isinstance(listing.entries, SavedResultEntries):
        if (listing.entries == () and listing.already_saved
                and (await runtime.database.read(lambda owned: runtime.capture.read_last_result_page(listing.ticket, owned))) is None):
            return
        raise ConsistencyError("分段策略要求可靠原始结果页")
    source = await runtime.database.read(lambda owned: runtime.capture.read_result_source(listing.ticket, owned))
    context = source.context
    activity_id = int(listing.ticket.target_id)
    activity = await runtime.database.read(lambda owned: row_facts(owned.connection, "device_activities", activity_id))
    async for saved in listing.entries.pages():
        ids = dict(saved.file_ids)
        for entry in saved.page.entries:
            if entry.identity not in ids:
                continue
            file_id = ids[entry.identity]
            file = await runtime.database.read(lambda owned: row_facts(owned.connection, "device_files", file_id))
            if file["presence_state"] != int(
                enum_for("device_files.presence_state").PRESENT
            ):
                await _save(
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
            await _save(
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
            await _save(runtime, action_id, runtime.capture, "save_file_completion", request)
    if listing.scan_complete and listing.set_finalized:
        from camctl.persistence.repositories.segment_result_sets import _boundary
        from camctl.persistence.repositories.result_file_pairing import (
            read_pairing_fact,
        )

        async for saved in listing.entries.pages():
            for identity, file_id in saved.file_ids:
                if identity.rpartition(".")[2].lower() != "lrf":
                    continue
                existing = await runtime.database.read(lambda owned: owned.connection.execute(
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
                ).fetchone())
                if existing is not None:
                    await runtime.database.read(lambda owned: read_pairing_fact(
                        owned.connection,
                        existing[0],
                        file_id,
                        _boundary(owned.connection),
                    ))
                    continue
                await _save(
                    runtime,
                    action_id,
                    runtime.capture,
                    "save_result_file_pairing",
                    (await runtime.database.read(lambda owned, occurred_at=runtime.wall_us(): PairResultFilesSave(
                        file_id,
                        listing.entries.last_ref,
                        _boundary(owned.connection),
                        occurred_at,
                    ))),
                )


async def _members(repository, ref, runtime):
    cursor = None
    while True:
        page = await runtime.database.read(lambda owned: repository.read_members(ref, cursor, 128, owned))
        for item in page.items:
            yield item
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


async def _prepare_members(runtime, repository, ref):
    row = await runtime.database.read(lambda owned: repository.read_result_set(ref, owned))
    if row["state"] != int(enum_for("segment_sets.state").COLLECTING):
        return
    from camctl.capture.segment_storage import ResultMemberInputCursor

    previous = (await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT MAX(file_id) FROM segment_members WHERE set_id=?", (ref.set_id,)
    ).fetchone()))[0]
    cursor = None if previous is None else ResultMemberInputCursor(ref, previous)
    while True:
        page = await runtime.database.read(lambda owned: repository.read_result_member_inputs(ref, cursor, 128, owned))
        if page.items:
            await _save(
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
    await _save(
        runtime, ref.action_id, repository, "fix_result_members", ref, runtime.wall_us()
    )


async def advance_segmented_recording(runtime, action, decision):
    """普通分段结果在同一发布事务登记全部产物、默认范围和动作结果。"""
    from camctl.capture import handlers as common

    action_id = action["id"]
    await resume_segment_writes(runtime.database, runtime.pending_segment_writes, action_id)
    repository = SegmentRepository()
    ref = await runtime.database.read(lambda owned: repository.read_prepared_result_ref(action_id, owned))
    activity_id = await common._activity_id_of(runtime, action_id)
    activity = await runtime.database.read(lambda owned: row_facts(
        owned.connection, "device_activities", activity_id,
    ))
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
            listing = await common._saved_result_listing(runtime, action_id)
        await register_segment_observations(runtime, action_id, listing)
        if (await runtime.action(action_id))["cancel_requested"]:
            await common._finish_listing_result(runtime, listing, finalize_output_set=True)
            await release_segment_occupancy(runtime, action_id)
            await finish_segmented_cancellation(runtime, action_id)
            return
        used = (await runtime.database.read(lambda owned: row_facts(
            owned.connection, "operation_runs", listing.ticket.run_id
        )))["attempts_used"]
        explicit_error = (
            listing.outcome is not None and listing.outcome.error is not None
        )
        if (
            not explicit_error
            and used < runtime.check_config.max_attempts
            and not listing.already_saved
        ):
            from camctl.capture.segment_preflight import assess_current_listing

            assessment = await assess_current_listing(
                runtime, action, listing, control, overrun
            )
            if assessment is None or assessment.status not in (
                SegmentAssessmentStatus.SUCCEEDED,
                SegmentAssessmentStatus.FAILED,
            ):
                await common._finish_listing_result(runtime, listing, retry_wait=True)
                return
        await common._finish_listing_result(runtime, listing, finalize_output_set=True)
        request = await runtime.database.read(lambda owned, occurred_at=runtime.wall_us(): repository.prepare_result_set(
            action_id, occurred_at, owned
        ))
        ref = await _save(runtime, action_id, repository, "create_result_set", request)
    else:
        # 原组已开始固定后重入，目录结束仍沿原末页与原保存责任核实。
        listing = await common._saved_result_listing(runtime, action_id)
        await common._finish_listing_result(runtime, listing, finalize_output_set=True)
    await release_segment_occupancy(runtime, action_id)
    await _prepare_members(runtime, repository, ref)
    scan_ref = await runtime.database.read(lambda owned: repository.read_current_segment_ref(ref, owned))
    setrow = await runtime.database.read(lambda owned: repository.read_result_set(ref, owned))
    from camctl.capture.segment_storage import ResultCheckConclusionSave

    reason, _, current_failure = await runtime.database.read(lambda owned: current_decision(owned.connection, setrow))
    found = await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT id FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
    ).fetchone())
    if found is None:
        (anchor,) = await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT result_event_id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
            (setrow["run_id"],),
        ).fetchone())
        await _save(
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
        saved_count = (await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT COUNT(*) FROM segment_gaps WHERE set_id=?", (ref.set_id,)
        ).fetchone()))[0]
        batch = []
        if setrow["state"] == int(enum_for("segment_sets.state").MEMBERS_FIXED):
            from camctl.persistence.repositories.segment_listing import materialized_current_members
            from camctl.persistence.repositories.segment_current import current_assessment

            current = await runtime.database.read(lambda owned: current_assessment(
                owned.connection, setrow))
            if current is not None and current.gap_count is not None:
                async with materialized_current_members(runtime.database, setrow) as members:
                    gaps = iter_segment_gaps(
                        members(), last_nonzero_cursor=current.last_nonzero_cursor,
                        last_owned_cursor=current.last_owned_cursor,
                        start_key=current.confirmed_start_key,
                    )
                    for number, gap in enumerate(gaps):
                        if number >= saved_count:
                            batch.append(gap)
                            if len(batch) == 128:
                                await _save(
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
                await _save(
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
            await _save(
                runtime, action_id, repository, "decide_result", ref, runtime.wall_us()
            )
        await _save(
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
        start = await runtime.database.read(lambda owned: repository.read_start(ref, owned))
        stop = (await common._stop_confirmed_at(runtime, action_id)) is not None
        completion = await common._record_file_completion_context(runtime, action_id)
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
        async for stored in _members(repository, scan_ref, runtime):
            state = advance_segment_assessment(state, stored.member)
        assessment = finish_segment_assessment(state, facts)
        saved_count = (await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT COUNT(*) FROM segment_gaps WHERE set_id=?", (ref.set_id,)
        ).fetchone()))[0]
        batch = []
        if assessment.gap_count is not None:
            from camctl.persistence.repositories.segment_listing import materialized_segment_members

            async with materialized_segment_members(runtime.database, repository, scan_ref) as members:
                for number, gap in enumerate(iter_segment_gaps(
                    (stored.member for stored in members()),
                    last_nonzero_cursor=assessment.last_nonzero_cursor,
                    last_owned_cursor=assessment.last_owned_cursor,
                    start_key=start.key,
                )):
                    if number < saved_count:
                        continue
                    batch.append(gap)
                    if len(batch) == 128:
                        await _save(
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
            await _save(
                runtime,
                action_id,
                repository,
                "append_gaps",
                scan_ref,
                tuple(batch),
                runtime.wall_us(),
            )
        await _save(
            runtime,
            action_id,
            repository,
            "save_assessment",
            scan_ref,
            facts,
            assessment,
            runtime.wall_us(),
        )
    assessment = await runtime.database.read(lambda owned: repository.read_assessment(scan_ref, owned))
    if assessment.default_scope.state is DefaultScopeState.NONEMPTY:
        count = (await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members WHERE set_id=?", (ref.set_id,)
        ).fetchone()))[0]
        selected = 0
        batch = []
        from camctl.persistence.repositories.segments import _eligible_original

        async for stored in _members(repository, scan_ref, runtime):
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
                    await _save(
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
            await _save(
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

            original_count = 0
            async for stored in _members(repository, scan_ref, runtime):
                original_count += _eligible_original(stored.member)
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
    await _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            scan_ref, activity["id"], setrow["run_id"], runtime.wall_us(), failure
        ),
    )


async def finish_segmented_cancellation(runtime, action_id):
    """取消接手同一原观察边界，发布零产物的明确取消版本。"""
    from camctl.capture.segment_storage import ResultCheckConclusionSave
    from camctl.capture.segment_models import PublicationKind, ResultCheckEndReason
    from camctl.contracts.history_values import HistoryBoundary

    await resume_segment_writes(runtime.database, runtime.pending_segment_writes, action_id)
    from camctl.operations.attempts import StaleRunFinish, RunOutcome

    if (await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.action_id=? AND t.status=1 LIMIT 1",
        (action_id,),
    ).fetchone())):
        raise ConsistencyError("分段取消须先可靠结束原在途调用")
    stale = await runtime.database.read(lambda owned: tuple(
        row[0]
        for row in owned.connection.execute(
            "SELECT responsibility_key FROM operation_runs"
            " WHERE action_id=? AND kind!=? AND status IN (1,2) ORDER BY id",
            (action_id, int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS)),
        )
    ))
    if stale:
        await _save(
            runtime,
            action_id,
            runtime.operations,
            "finish_stale_runs",
            StaleRunFinish(stale, RunOutcome.CANCELED, runtime.wall_us()),
        )
    repository = SegmentRepository()
    cancel = await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT e.id FROM history_events e JOIN json_each(e.body_json,'$.rows') r "
        "WHERE json_extract(r.value,'$.table')='actions' AND json_extract(r.value,'$.id')=? "
        "AND json_extract(r.value,'$.before.values.cancel_requested')=0 "
        "AND json_extract(r.value,'$.after.values.cancel_requested')=1 ORDER BY e.id DESC LIMIT 1",
        (action_id,),
    ).fetchone())
    if cancel is None:
        raise ConsistencyError("分段取消缺少实际取消请求")
    previous = await runtime.database.read(lambda owned: repository.read_prepared_result_ref(action_id, owned))
    predecessor = None if previous is None else previous.set_id
    ref = await runtime.database.read(lambda owned: repository.find_canceled_result_ref(
        action_id, predecessor, cancel[0], owned
    ))
    if ref is None:
        request = await runtime.database.read(lambda owned, occurred_at=runtime.wall_us(): repository.prepare_result_set(
            action_id, occurred_at, owned
        ))
        if previous is not None:
            row = await runtime.database.read(lambda owned: repository.read_result_set(previous, owned))
            from camctl.persistence.repositories.result_pages import _at

            request = await runtime.database.read(lambda owned: replace(
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
                    else _at(owned.connection, row["current_final_page_event_id"])[0].ref
                ),
            ))
        request = replace(
            request,
            publication_kind=PublicationKind.CANCELED,
            predecessor_set_id=predecessor,
            cancel_event_id=cancel[0],
        )
        ref = await _save(runtime, action_id, repository, "create_result_set", request)
    await _prepare_members(runtime, repository, ref)
    row = await runtime.database.read(lambda owned: repository.read_result_set(ref, owned))
    conclusion = await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT id FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
    ).fetchone())
    if conclusion is None:
        run = (
            None
            if row["run_id"] is None
            else (await runtime.database.read(lambda owned: row_facts(owned.connection, "operation_runs", row["run_id"])))
        )
        prior_end = None
        if run is not None and run["status"] not in (1, 2):
            (prior_end,) = await runtime.database.read(lambda owned: owned.connection.execute(
                "SELECT e.id FROM history_events e JOIN json_each(e.body_json,'$.rows') r "
                "WHERE json_extract(r.value,'$.table')='operation_runs' AND json_extract(r.value,'$.id')=? "
                "AND json_extract(r.value,'$.before.values.status') IN (1,2) "
                "AND json_extract(r.value,'$.after.values.status') IN (3,4,5,6,7) ORDER BY e.id DESC LIMIT 1",
                (run["id"],),
            ).fetchone())
        prior = (
            None
            if predecessor is None
            else (await runtime.database.read(lambda owned: owned.connection.execute(
                "SELECT id FROM segment_check_conclusions WHERE set_id=?",
                (predecessor,),
            ).fetchone()))
        )
        await _save(
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
        await _save(runtime, action_id, repository, "decide_result", ref, runtime.wall_us())
    await _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            ref, row["activity_id"], row["run_id"], runtime.wall_us()
        ),
    )


async def finish_segmented_baseline_failure(runtime, action_id):
    """基准实际失败也发布明确的无 RESULTS 来源，不派发 START。"""
    return await _finish_segmented_setup_failure(runtime, action_id, preparation=False)


async def finish_segmented_preparation_failure(runtime, action_id):
    """设置实际失败以原 PREPARE 结果发布无 RESULTS 结论。"""
    return await _finish_segmented_setup_failure(runtime, action_id, preparation=True)


async def resume_segmented_preparation_failure(runtime, action):
    """原无 RESULTS 准备决定先发布，不让冷恢复取得新的设备资格。"""
    if action["execution_spec_json"].get("result_policy") is None or action["cancel_requested"]:
        return False
    await resume_segment_writes(runtime.database, runtime.pending_segment_writes, action["id"])
    repository = SegmentRepository()
    ref = await runtime.database.read(lambda owned: repository.read_prepared_result_ref(action["id"], owned))
    if ref is None:
        return False
    row = await runtime.database.read(lambda owned: repository.read_result_set(ref, owned))
    if row["source_kind"] != int(enum_for("segment_sets.source_kind").NO_RESULTS):
        return False
    from camctl.capture.segment_models import ResultCheckEndReason
    conclusion = await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT c.anchor_event_id FROM segment_check_conclusions c WHERE c.set_id=? AND c.reason=?",
        (ref.set_id, int(enum_for("segment_check_conclusions.reason")[ResultCheckEndReason.NO_RESULT_WORK.name])),
    ).fetchone())
    if conclusion is None:
        return False
    prep = await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT r.id FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
        " WHERE r.action_id=? AND r.kind=? AND t.result_event_id=?",
        (action["id"], int(enum_for("operation_runs.kind").PREPARE_CAPTURE), conclusion[0]),
    ).fetchone())
    if prep is None:
        return False
    from camctl.persistence.repositories.segment_check_conclusions import no_result_preparation_failure
    await runtime.database.read(lambda owned: no_result_preparation_failure(owned.connection, row, conclusion[0]))
    await finish_segmented_preparation_failure(runtime, action["id"])
    return True


async def _finish_segmented_setup_failure(runtime, action_id, *, preparation):
    from camctl.capture.segment_storage import ResultCheckConclusionSave
    from camctl.capture.segment_models import ResultCheckEndReason

    await resume_segment_writes(runtime.database, runtime.pending_segment_writes, action_id)
    repository = SegmentRepository()
    ref = await runtime.database.read(lambda owned: repository.read_prepared_result_ref(action_id, owned))
    if ref is None:
        ref = await _save(
            runtime,
            action_id,
            repository,
            "create_result_set",
            (await runtime.database.read(lambda owned, occurred_at=runtime.wall_us(): repository.prepare_result_set(action_id, occurred_at, owned))),
        )
    await _prepare_members(runtime, repository, ref)
    row = await runtime.database.read(lambda owned: repository.read_result_set(ref, owned))
    if (
        (await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT 1 FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
        ).fetchone()))
        is None
    ):
        if preparation:
            (anchor,) = await runtime.database.read(lambda owned: owned.connection.execute(
                "SELECT t.result_event_id FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
                " WHERE r.action_id=? AND r.kind=? ORDER BY r.id DESC,t.attempt_no DESC LIMIT 1",
                (action_id, int(enum_for("operation_runs.kind").PREPARE_CAPTURE)),
            ).fetchone())
        else:
            (anchor,) = await runtime.database.read(lambda owned: owned.connection.execute(
                "SELECT result_event_id FROM baseline_read_calls WHERE action_id=? ORDER BY read_no DESC LIMIT 1",
                (action_id,),
            ).fetchone())
        await _save(
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
        await _save(runtime, action_id, repository, "decide_result", ref, runtime.wall_us())
    await _save(
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
                    "reason": "device_failed" if preparation else "baseline_read_failed",
                },
            ),
        ),
    )


async def finish_segmented_start_failure(runtime, action_id):
    """START 实际结束但没有 RESULTS 时，发布未知范围及原启动失败。"""
    from camctl.capture.segment_storage import ResultCheckConclusionSave
    from camctl.capture.segment_models import ResultCheckEndReason
    from camctl.persistence.repositories.segment_check_conclusions import (
        no_result_start_failure,
    )

    await resume_segment_writes(runtime.database, runtime.pending_segment_writes, action_id)
    if (await runtime.action(action_id))["status"] in (3, 4, 5, 6):
        return
    repository = SegmentRepository()
    ref = await runtime.database.read(lambda owned: repository.read_prepared_result_ref(action_id, owned))
    if ref is None:
        ref = await _save(
            runtime,
            action_id,
            repository,
            "create_result_set",
            (await runtime.database.read(lambda owned, occurred_at=runtime.wall_us(): repository.prepare_result_set(action_id, occurred_at, owned))),
        )
    await _prepare_members(runtime, repository, ref)
    row = await runtime.database.read(lambda owned: repository.read_result_set(ref, owned))
    (anchor,) = await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT t.result_event_id FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.action_id=? AND r.kind=? ORDER BY t.attempt_no DESC LIMIT 1",
        (action_id, int(enum_for("operation_runs.kind").START)),
    ).fetchone())
    failure = await runtime.database.read(lambda owned: no_result_start_failure(owned.connection, row, anchor))
    if (
        (await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT 1 FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
        ).fetchone()))
        is None
    ):
        await _save(
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
        await _save(runtime, action_id, repository, "decide_result", ref, runtime.wall_us())
    await _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            ref, row["activity_id"], None, runtime.wall_us(), failure
        ),
    )


async def resume_closed_start(runtime, action):
    """冷恢复接手原 START 结束，不重新授予启动或更换已保存错误。"""
    if action["execution_spec_json"].get("result_policy") is None:
        return False
    run = await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT status FROM operation_runs WHERE action_id=? AND kind=?",
        (action["id"], int(enum_for("operation_runs.kind").START)),
    ).fetchone())
    results = await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT id FROM operation_runs WHERE action_id=? AND kind=?",
        (action["id"], int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS)),
    ).fetchone())
    if run is None or run[0] not in (4, 6) or results is not None:
        return False
    if action["cancel_requested"]:
        await finish_segmented_cancellation(runtime, action["id"])
    else:
        await finish_segmented_start_failure(runtime, action["id"])
    return True


async def finish_segmented_external_failure(runtime, action_id, reason, failure, anchor=None):
    """外部收场仅接手已保存的观察，按同一发布边界保留合格文件。"""
    from camctl.capture.segment_storage import ResultCheckConclusionSave
    from camctl.capture.segment_models import ResultCheckEndReason

    await resume_segment_writes(runtime.database, runtime.pending_segment_writes, action_id)
    repository = SegmentRepository()
    ref = await runtime.database.read(lambda owned: repository.read_prepared_result_ref(action_id, owned))
    if ref is None:
        ref = await _save(
            runtime,
            action_id,
            repository,
            "create_result_set",
            (await runtime.database.read(lambda owned, occurred_at=runtime.wall_us(): repository.prepare_result_set(action_id, occurred_at, owned))),
        )
    await _prepare_members(runtime, repository, ref)
    row = await runtime.database.read(lambda owned: repository.read_result_set(ref, owned))
    if (
        (await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT 1 FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
        ).fetchone()))
        is None
    ):
        await _save(
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
        await _save(runtime, action_id, repository, "decide_result", ref, runtime.wall_us())
    await _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            ref, row["activity_id"], row["run_id"], runtime.wall_us(), failure
        ),
    )
