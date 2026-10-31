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
from camctl.persistence.repositories.segment_native import read_saved_native_classification
from camctl.persistence.repositories.segments import SegmentRepository
from camctl.persistence.transaction import row_facts


async def advance_native_result(runtime, action_id, *, canceled=False):
    from camctl.capture import handlers as common
    from camctl.persistence.repositories.segment_listing import ordered_listing_members

    await resume_segment_writes(runtime.database, runtime.pending_segment_writes, action_id)
    repository = SegmentRepository()
    ref = await runtime.database.read(lambda owned: repository.read_prepared_result_ref(action_id, owned))
    listing = None
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
        if (await runtime.action(action_id))["cancel_requested"] and not canceled:
            await common._finish_listing_result(runtime, listing, retry_wait=True)
            return
        used = (await runtime.database.read(lambda owned: row_facts(owned.connection, "operation_runs", listing.ticket.run_id)))["attempts_used"]
        prepared = await runtime.database.read(lambda owned, occurred_at=runtime.wall_us(): repository.prepare_result_set(
            action_id, occurred_at, owned
        ))
        async with ordered_listing_members(runtime.database, listing) as members:
            classified = classify_native_members(
                members,
                complete=prepared.source_kind is SegmentSourceKind.COMPLETE_SCAN,
                activity_id=prepared.activity_id,
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
            await common._finish_listing_result(runtime, listing, retry_wait=True)
            return
        await common._finish_listing_result(runtime, listing, finalize_output_set=True)
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
        (anchor,) = await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT result_event_id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
            (row["run_id"],),
        ).fetchone())
        await _save(
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
    classified = await runtime.database.read(lambda owned: read_saved_native_classification(owned.connection, row))
    if classified.current_result is CurrentSegmentResult.SUFFICIENT:
        listing = await common._saved_result_listing(runtime, action_id)
        await common._finish_listing_result(runtime, listing, finalize_output_set=True)
        activity = await runtime.database.read(lambda owned: row_facts(owned.connection, "device_activities", row["activity_id"]))
        if canceled and activity["activity_state"] != 3:
            from camctl.persistence.repositories import finite_cancel

            if (
                (await common._result_source(runtime, listing.ticket)).kind
                is common.ResultSourceKind.NATURAL
            ):
                await _save(
                    runtime,
                    action_id,
                    finite_cancel,
                    "save_finite_cancel_end",
                    finite_cancel.FiniteCancelEndSave(
                        action_id, listing.entries.last_ref, runtime.wall_us()
                    ),
                )
                activity = await runtime.database.read(lambda owned: row_facts(owned.connection, "device_activities", row["activity_id"]))
        if not canceled and activity["result_set_state"] not in (3, 4):
            first = (await anext(listing.entries.pages())).ref
            observation = {
                "first_page_event_id": first.event_id,
                "last_page_event_id": listing.entries.last_ref.event_id,
                "file_count": row["member_count"],
            }
            completion = await runtime.database.read(lambda owned: runtime.capture.read_result_completion(
                action_id, owned
            ))
            if completion is None:
                completion = {
                    "method": common._TIME_AND_OUTPUTS_METHOD,
                    "wait_completed_event_id": activity["wait_completed_event_id"],
                    "observation": observation,
                }
            await _save(
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
        await release_segment_occupancy(runtime, action_id)
    if row["state"] == 2:
        await _save(runtime, action_id, repository, "decide_result", ref, runtime.wall_us())
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
    await _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            ref, row["activity_id"], row["run_id"], runtime.wall_us(), failure
        ),
    )
