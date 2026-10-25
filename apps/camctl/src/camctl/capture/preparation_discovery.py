"""调度发现与会话交接共用的原准备占用资格。"""

from camctl.contracts.enums import enum_for


def preparation_responsibility_where(*, include_pending=False):
    """SQL 固定使用动作 a、活动 d；只包含有原准备依据且可靠未 START 的占用。"""
    actions = enum_for("actions.status")
    types = enum_for("actions.type")
    activities = enum_for("device_activities.activity_state")
    occupancy = enum_for("device_activities.occupancy_state")
    dispatch = enum_for("device_activities.dispatch_state")
    runs = enum_for("operation_runs.kind")
    run_status = enum_for("operation_runs.status")
    attempts = enum_for("operation_attempts.status")
    effect = enum_for("operation_attempts.effect_state")
    purposes = enum_for("operation_runs.query_purpose")
    statuses = [actions.SUCCEEDED, actions.FAILED, actions.EXPIRED, actions.CANCELED]
    if include_pending:
        statuses.append(actions.PENDING)
    encoded = ",".join(str(int(value)) for value in statuses)
    return (
        f"a.type IN ({int(types.CAMERA_RECORD)},{int(types.CAMERA_TIMELAPSE)})"
        f" AND a.status IN ({encoded}) AND d.occupancy_state={int(occupancy.HELD)}"
        f" AND d.activity_state={int(activities.UNKNOWN)} AND d.started_at IS NULL"
        f" AND d.dispatch_state IN ({int(dispatch.NOT_DISPATCHED)},{int(dispatch.REJECTED_WITHOUT_EFFECT)})"
        " AND (a.execution_started=0 OR EXISTS (SELECT 1 FROM operation_runs prep WHERE prep.activity_id=d.id"
        f" AND prep.kind={int(runs.PREPARE_CAPTURE)}))"
        " AND NOT EXISTS (SELECT 1 FROM operation_runs sr JOIN operation_attempts st ON st.run_id=sr.id"
        f" WHERE sr.action_id=a.id AND sr.kind={int(runs.START)} AND (st.status={int(attempts.RUNNING)}"
        f" OR st.effect_state<>{int(effect.NO_EFFECT)} OR st.result_event_id IS NULL))"
        " AND NOT EXISTS (SELECT 1 FROM operation_runs cq WHERE cq.action_id=a.id"
        f" AND cq.kind={int(runs.QUERY_ACTIVITY)} AND cq.query_purpose={int(purposes.START_CONFIRMATION)}"
        f" AND cq.status IN ({int(run_status.PENDING)},{int(run_status.ACTIVE)}))"
    )
