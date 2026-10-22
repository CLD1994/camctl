"""原生有限任务独立发布空尾分类，复用原自然等待与取消收场。"""

from camctl.capture.segment_completion import (
    _save,
    _prepare_members,
    register_segment_observations,
    release_segment_occupancy,
    resume_segment_writes,
)
from camctl.capture.segment_finish import FinishSegmentRecording
from camctl.capture.segment_storage import ResultCheckConclusionSave, SegmentSourceKind
from camctl.capture.segment_models import ResultCheckEndReason, CurrentSegmentResult
from camctl.capture.native_segments import classify_native_members
from camctl.persistence.repositories.segment_native import native_classification
from camctl.persistence.repositories.segments import SegmentRepository
from camctl.persistence.transaction import row_facts


async def advance_native_result(runtime, action_id, *, canceled=False):
    from camctl.capture import handlers as common
    from camctl.capture.segment_preflight import ordered_listing_members

    resume_segment_writes(runtime.owned, runtime.pending_segment_writes, action_id)
    db = runtime.owned.connection
    repository = SegmentRepository()
    ref = repository.read_prepared_result_ref(action_id, runtime.owned)
    listing = None
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
        if runtime.action(action_id)["cancel_requested"] and not canceled:
            common._finish_listing_result(runtime, listing, retry_wait=True)
            return
        used = row_facts(db, "operation_runs", listing.ticket.run_id)["attempts_used"]
        prepared = repository.prepare_result_set(
            action_id, runtime.wall_us(), runtime.owned
        )
        with ordered_listing_members(runtime, listing) as members:
            classified = classify_native_members(
                members,
                complete=prepared.source_kind is SegmentSourceKind.COMPLETE_SCAN,
                activity_id=prepared.activity_id,
                explicit_error=listing.outcome is not None
                and listing.outcome.error is not None,
            )
        if (
            used < runtime.check_config.max_attempts
            and not listing.already_saved
            and (
                classified.current_result is CurrentSegmentResult.UNCONFIRMED
                or canceled
                and listing.outcome is not None
                and listing.outcome.error is not None
            )
        ):
            common._finish_listing_result(runtime, listing, retry_wait=True)
            return
        common._finish_listing_result(runtime, listing, finalize_output_set=True)
        ref = _save(
            runtime,
            action_id,
            repository,
            "create_result_set",
            repository.prepare_result_set(action_id, runtime.wall_us(), runtime.owned),
        )
    _prepare_members(runtime, repository, ref)
    row = repository.read_result_set(ref, runtime.owned)
    classified = native_classification(db, row)
    if (
        db.execute(
            "SELECT 1 FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
        ).fetchone()
        is None
    ):
        (anchor,) = db.execute(
            "SELECT result_event_id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
            (row["run_id"],),
        ).fetchone()
        _save(
            runtime,
            action_id,
            repository,
            "conclude_result_check",
            ResultCheckConclusionSave(
                ref,
                ResultCheckEndReason.NATIVE_FINITE_CANCEL_END
                if canceled
                else ResultCheckEndReason.NATIVE_RESULT_DECIDED,
                anchor,
                None,
                runtime.check_config,
                None,
                None,
                runtime.wall_us(),
            ),
        )
    if classified.current_result is CurrentSegmentResult.SUFFICIENT:
        listing = common._saved_result_listing(runtime, action_id)
        common._finish_listing_result(runtime, listing, finalize_output_set=True)
        activity = row_facts(db, "device_activities", row["activity_id"])
        if canceled and activity["activity_state"] != 3:
            from camctl.persistence.repositories import finite_cancel

            if (
                common._result_source(runtime, listing.ticket).kind
                is common.ResultSourceKind.NATURAL
            ):
                _save(
                    runtime,
                    action_id,
                    finite_cancel,
                    "save_finite_cancel_end",
                    finite_cancel.FiniteCancelEndSave(
                        action_id, listing.entries.last_ref, runtime.wall_us()
                    ),
                )
                activity = row_facts(db, "device_activities", row["activity_id"])
        if not canceled and activity["result_set_state"] not in (3, 4):
            first = next(listing.entries.pages()).ref
            observation = {
                "first_page_event_id": first.event_id,
                "last_page_event_id": listing.entries.last_ref.event_id,
                "file_count": row["member_count"],
            }
            completion = runtime.capture.read_result_completion(
                action_id, runtime.owned
            )
            if completion is None:
                completion = {
                    "method": common._TIME_AND_OUTPUTS_METHOD,
                    "wait_completed_event_id": activity["wait_completed_event_id"],
                    "observation": observation,
                }
            _save(
                runtime,
                action_id,
                runtime.capture,
                "confirm_result_set",
                common.ResultSetSave(
                    action_id,
                    runtime.wall_us(),
                    common.ResultSetPhase.COMPLETE,
                    "result_files_listed/v2",
                    observation,
                    {"status": "completed"},
                    completion,
                ),
            )
        release_segment_occupancy(runtime, action_id)
    if row["state"] == 2:
        _save(runtime, action_id, repository, "decide_result", ref, runtime.wall_us())
    failure = (
        None
        if canceled or classified.current_result is CurrentSegmentResult.SUFFICIENT
        else classified.failure
    )
    if (
        failure is None
        and not canceled
        and classified.current_result is not CurrentSegmentResult.SUFFICIENT
    ):
        from camctl.capture.media import RecordingFailure

        failure = RecordingFailure(
            "capture_result_unconfirmed",
            {"activity_id": str(row["activity_id"]), "reason": "outputs_unknown"},
        )
    _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            ref, row["activity_id"], row["run_id"], runtime.wall_us(), failure
        ),
    )
