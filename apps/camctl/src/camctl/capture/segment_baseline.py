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
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.models import ErrorValue


async def consume_read(pending, runtime):
    owner = pending.read_owner
    if owner is None:
        return True
    result = (await resume_baseline_read_save(
        owner, capture=runtime.capture, database=runtime.database
    ))
    if owner.wakeup_pending:
        owner.wakeup_pending = False
        pending.wakeup_pending = True
    if result is None:
        if (owner.aborted_before_call and owner.intent_view is None
                and owner.saves.pending is None):
            pending.aborted = True
            pending.read_owner = None
            return True
        return False
    pending.aborted = owner.aborted_before_call
    pending.read_no = result.request.read_no
    pending.cursor = result.next_cursor
    pending.exhausted = (
        result.error is None
        and result.state is BaselineReadState.RETURNED
        and result.next_cursor is None
    )
    if result.error is not None:
        pending.error = result.error
        pending.diagnostic = owner.error_diagnostic
    elif result.state is BaselineReadState.UNCONFIRMED:
        pending.error = ErrorValue(
            "capture_result_unconfirmed",
            "execution",
            {"activity_id": str(pending.activity_id), "reason": "outputs_unknown"},
        )
    pending.read_owner = None
    return True


async def prepare_segment_baseline(action_id, activity_id, runtime):
    from camctl.capture.baseline import _with_wakeup

    pending = runtime.pending_baselines.get(action_id)
    result = await _prepare_segment_baseline(action_id, activity_id, runtime)
    return _with_wakeup(result, pending, runtime.pending_baselines.get(action_id))


async def _prepare_segment_baseline(action_id, activity_id, runtime):
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

    pending = runtime.pending_baselines.get(action_id)
    if pending is None:
        action = (await runtime.database.read(lambda owned: row_facts(owned.connection, "actions", action_id)))
        activity = (await runtime.database.read(lambda owned: row_facts(owned.connection, "device_activities", activity_id)))
        if activity["ownership_mode"] != 2:
            return BaselinePreparation(PreparationPhase.READY, wakeup_required=False)
        ref = (await runtime.database.read(lambda owned, inputs=(activity_id,), options=dict(): runtime.capture.baseline_ref(*inputs, owned, **options), description="读取已保存拍摄事实"))
        if ref.state.name == "FIXED":
            return BaselinePreparation(PreparationPhase.READY, wakeup_required=False)
        pending = PendingBaseline(
            activity_id,
            DeviceBinding(action["device_id"], action["driver_id"]),
            tuple(activity["output_scope_json"]["directories"]),
        )
        runtime.pending_baselines[action_id] = pending
        found = (await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT id FROM baseline_read_calls WHERE activity_id=? ORDER BY read_no DESC LIMIT 1",
            (activity_id,),
        ).fetchone()))
        if found is not None:
            view = (await runtime.database.read(lambda owned, inputs=(found[0], (await runtime.database.read(lambda owned: _boundary(owned.connection)))), options=dict(): runtime.capture.read_baseline_call(*inputs, owned, **options), description="读取已保存拍摄事实"))
            pending.read_owner = PendingBaselineRead(
                view.request,
                intent_view=view,
                result=None if view.state is BaselineReadState.INTENDED else view,
                locally_created=False,
            )
            if view.state is BaselineReadState.INTENDED:
                (await resume_baseline_read_call(
                    pending.read_owner,
                    BaselineReadRecoveryContext(
                        runtime.database.metadata.instance_id,
                        view,
                        {activity_id: pending.read_owner},
                        runtime.recovery_boundary,
                        runtime.recovery_max_event_id,
                    ),
                    capture=runtime.capture,
                    database=runtime.database,
                    wall_us=runtime.wall_us,
                ))
    if pending.activity_id != activity_id:
        raise ConsistencyError("基准准备拥有者不得更换原活动")
    resumed = (await resume_baseline_save(action_id, runtime=runtime))
    if resumed is not None:
        owner = pending.read_owner
        if not (resumed.phase is PreparationPhase.WAITING and owner is not None
                and owner.locally_created and owner.task is None and not pending.in_call):
            return resumed
        pending.wakeup_pending |= resumed.wakeup_required
    if pending.aborted:
        return BaselinePreparation(PreparationPhase.ABORTED)
    if pending.error is not None:
        return BaselinePreparation(PreparationPhase.FAILED, error=pending.error)

    async def can_read():
        def read(owned):
            current = row_facts(owned.connection, "actions", action_id)
            if current is None:
                raise LookupError(f"动作不存在: {action_id}")
            return current, row_facts(owned.connection, "device_activities", activity_id)
        current, activity = await runtime.database.read(
            read, description="读取分段基准派发资格")
        return (
            not current["cancel_requested"]
            and (current["status"], current["execution_started"]) in ((1, 0), (2, 1))
            and activity is not None and activity["action_id"] == action_id
            and activity["occupancy_state"] == 1 and activity["dispatch_state"] == 1
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
        if not await can_read() and pending.read_owner is None:
            return BaselinePreparation(PreparationPhase.ABORTED)
        ref = (await runtime.database.read(lambda owned, inputs=(activity_id,), options=dict(): runtime.capture.baseline_ref(*inputs, owned, **options), description="读取已保存拍摄事实"))
        if pending.exhausted:
            pending.saves.begin(
                BaselineFixSave(
                    activity_id, ref.chunk_count, ref.entry_count, runtime.wall_us()
                )
            )
            return (await resume_baseline_save(action_id, runtime=runtime))
        if runtime.baseline_directory is None:
            return BaselinePreparation(PreparationPhase.WAITING, wakeup_required=False)
        owner = pending.read_owner
        if owner is None:
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
                database=runtime.database,
                wall_us=runtime.wall_us,
                can_read=can_read,
                baseline=pending,
            )
        finally:
            pending.in_call = owner.task is not None and not owner.task.done()
        resumed = (await resume_baseline_save(action_id, runtime=runtime))
        if resumed is not None:
            return resumed
        if pending.aborted:
            return BaselinePreparation(PreparationPhase.ABORTED)
        if pending.error is not None:
            return BaselinePreparation(PreparationPhase.FAILED, error=pending.error)
