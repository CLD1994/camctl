"""结果策略任务的原目录调用接线；每次只持有一页和原保存身份。"""

from camctl.capture.baseline_models import (
    BaselineReadIntent,
    BaselineReadState,
    BaselineFixSave,
)
from camctl.capture.baseline_reads import (
    PendingBaselineRead,
    advance_baseline_read,
    resume_baseline_read_call,
    BaselineReadRecoveryContext,
    resume_baseline_read_save,
)
from camctl.contracts.values import new_operation_key
from camctl.operations.models import ErrorValue


def consume_read(pending, runtime):
    owner = pending.read_owner
    if owner is None:
        return True
    result = resume_baseline_read_save(
        owner, capture=runtime.capture, owned=runtime.owned
    )
    if result is None:
        return False
    pending.read_no = result.request.read_no
    pending.cursor = result.next_cursor
    pending.exhausted = (
        result.error is None
        and result.state is BaselineReadState.RETURNED
        and result.next_cursor is None
    )
    if result.error is not None:
        pending.error = result.error
    elif result.state is BaselineReadState.UNCONFIRMED:
        pending.error = ErrorValue(
            "capture_result_unconfirmed",
            "execution",
            {"activity_id": str(pending.activity_id), "reason": "outputs_unknown"},
        )
    pending.read_owner = None
    return True


async def prepare_segment_baseline(action_id, activity_id, runtime):
    from camctl.capture.baseline import (
        PendingBaseline,
        BaselinePreparation,
        PreparationPhase,
        resume_baseline_save,
    )
    from camctl.persistence.transaction import row_facts
    from camctl.persistence.repositories.segment_result_sets import _boundary
    from camctl.devices.bindings import DeviceBinding
    from camctl.scheduling.rules import LaunchWindow, WindowPhase, window_phase

    db = runtime.owned.connection
    pending = runtime.pending_baselines.get(action_id)
    if pending is None:
        action = row_facts(db, "actions", action_id)
        activity = row_facts(db, "device_activities", activity_id)
        if activity["ownership_mode"] != 2:
            return BaselinePreparation(PreparationPhase.READY)
        ref = runtime.capture.baseline_ref(activity_id, runtime.owned)
        if ref.state.name == "FIXED":
            return BaselinePreparation(PreparationPhase.READY)
        pending = PendingBaseline(
            activity_id,
            DeviceBinding(action["device_id"], action["driver_id"]),
            tuple(activity["output_scope_json"]["directories"]),
        )
        runtime.pending_baselines[action_id] = pending
        found = db.execute(
            "SELECT id FROM baseline_read_calls WHERE activity_id=? ORDER BY read_no DESC LIMIT 1",
            (activity_id,),
        ).fetchone()
        if found is not None:
            view = runtime.capture.read_baseline_call(
                found[0], _boundary(db), runtime.owned
            )
            pending.read_owner = PendingBaselineRead(
                view.request,
                intent_view=view,
                result=None if view.state is BaselineReadState.INTENDED else view,
            )
            if view.state is BaselineReadState.INTENDED:
                resume_baseline_read_call(
                    pending.read_owner,
                    BaselineReadRecoveryContext(
                        runtime.owned.metadata.instance_id,
                        view,
                        {activity_id: pending.read_owner},
                        runtime.recovery_boundary,
                        runtime.recovery_max_event_id,
                    ),
                    capture=runtime.capture,
                    owned=runtime.owned,
                    reader=runtime.owned,
                    wall_us=runtime.wall_us,
                )
    if not consume_read(pending, runtime):
        return BaselinePreparation(
            PreparationPhase.PENDING, database_error=pending.read_owner.diagnostic
        )
    resumed = resume_baseline_save(action_id, runtime=runtime)
    if resumed is not None:
        return resumed
    if pending.error is not None:
        return BaselinePreparation(PreparationPhase.FAILED, error=pending.error)

    def can_read():
        current = runtime.action(action_id)
        return (
            not current["cancel_requested"]
            and current["status"] == 2
            and window_phase(
                LaunchWindow(
                    current["scheduled_at"],
                    current["scheduled_at"] + current["max_delay_ms"] * 1000,
                ),
                runtime.wall_us(),
            )
            is not WindowPhase.AFTER_WINDOW
        )

    while True:
        if not can_read():
            return BaselinePreparation(PreparationPhase.ABORTED)
        ref = runtime.capture.baseline_ref(activity_id, runtime.owned)
        if pending.exhausted:
            pending.saves.begin(
                BaselineFixSave(
                    activity_id, ref.chunk_count, ref.entry_count, runtime.wall_us()
                )
            )
            return resume_baseline_save(action_id, runtime=runtime)
        if runtime.baseline_directory is None:
            return BaselinePreparation(PreparationPhase.PENDING)
        intent = BaselineReadIntent(
            action_id,
            activity_id,
            pending.binding,
            pending.directories,
            pending.cursor,
            runtime.baseline_timeout_s,
            ref,
            pending.read_no + 1,
            new_operation_key(),
            new_operation_key(),
            runtime.wall_us(),
        )
        owner = PendingBaselineRead(intent)
        pending.read_owner = owner
        pending.in_call = True
        try:
            await advance_baseline_read(
                owner,
                directory=runtime.baseline_directory,
                capture=runtime.capture,
                owned=runtime.owned,
                wall_us=runtime.wall_us,
                can_read=can_read,
            )
        finally:
            pending.in_call = owner.task is not None and not owner.task.done()
        if not consume_read(pending, runtime):
            return BaselinePreparation(
                PreparationPhase.PENDING, database_error=owner.diagnostic
            )
        if pending.error is not None:
            return BaselinePreparation(PreparationPhase.FAILED, error=pending.error)
