"""拍摄启动效果与本地终止共同使用的可靠事实。

动作进入 RUNNING 与设备启动分别判断。所有原启动尝试都可靠无
效果且没有待核实启动责任时，才能按未启动处理；调用错误本身不
提供这个依据。这里不执行设备操作，也不保存推测的设备观察。
"""

from contextlib import closing
from dataclasses import dataclass
from typing import Any, Mapping

from camctl.contracts.enums import enum_for
from camctl.devices.start_entry import is_hasselblad_photo, entry_from_rows, EntryPhase
from camctl.contracts.values import ConsistencyError
from camctl.persistence.transaction import event_envelope, row_facts, update_change

_DISPATCH = enum_for("device_activities.dispatch_state")
_ATTEMPT = enum_for("operation_attempts.status")
_EFFECT = enum_for("operation_attempts.effect_state")
_RUN = enum_for("operation_runs.status")
_KIND = enum_for("operation_runs.kind")
_PURPOSE = enum_for("operation_runs.query_purpose")


@dataclass(frozen=True)
class StartFacts:
    activity: dict[str, Any] | None
    run: dict[str, Any] | None
    attempts_used: int
    not_started: bool
    dispatch_state: int | None


def load_start_facts(connection, action: Mapping[str, Any], *, result_events=()) -> StartFacts:
    """核对本动作活动、全部启动尝试及尚未结束的启动核实责任。"""
    # 复合 START 结果事务只用已经形成并由内核共同校验的事件派生
    # 后续本地终止资格，不写入数据库或把当前 RUNNING 误认为已结束。
    projected = {}
    for event in result_events:
        for change in event.rows:
            identity = change.table, change.row_id
            projected.setdefault(identity, {}).update(change.after.values)

    def after(table, row):
        return None if row is None else {**row, **projected.get((table, row["id"]), {})}

    with closing(connection.execute(
        "SELECT id FROM device_activities WHERE action_id = ?", (action["id"],),
    )) as cursor:
        found = cursor.fetchone()
    activity = after("device_activities", None if found is None else row_facts(
        connection, "device_activities", found[0]))
    if action["execution_started"] and activity is None:
        raise ConsistencyError(f"已执行拍摄缺少设备活动: {action['id']}")
    with closing(connection.execute(
        "SELECT id FROM operation_runs WHERE responsibility_key = ?",
        (f"start/{action['id']}",),
    )) as cursor:
        found = cursor.fetchone()
    run = after("operation_runs", None if found is None else row_facts(
        connection, "operation_runs", found[0]))
    count = used = unresolved = dispatched = 0
    if run is not None:
        if (activity is None or run["kind"] != int(_KIND.START)
                or run["action_id"] != action["id"]
                or run["activity_id"] != activity["id"]):
            raise ConsistencyError(f"启动责任与动作或活动不符: {action['id']}")
        with closing(connection.execute(
            "SELECT COUNT(*), COALESCE(MAX(attempt_no), 0),"
            " COALESCE(SUM(status = ? OR effect_state != ? OR result_event_id IS NULL), 0),"
            " COALESCE(SUM(json_extract(result_json, '$.settlement.basis') != 'not_dispatched'), 0)"
            " FROM operation_attempts WHERE run_id = ?",
            (int(_ATTEMPT.RUNNING), int(_EFFECT.NO_EFFECT), run["id"]),
        )) as cursor:
            count, used, unresolved, dispatched = cursor.fetchone()
        if count != used or used != run["attempts_used"]:
            raise ConsistencyError(f"启动累计次数与原尝试不符: {action['id']}")
        for (table, row_id), values in projected.items():
            if table != "operation_attempts":
                continue
            original = row_facts(connection, table, row_id)
            if original is None or original["run_id"] != run["id"]:
                continue
            final = {**original, **values}

            def unresolved_result(row):
                return (row["status"] == int(_ATTEMPT.RUNNING)
                        or row["effect_state"] != int(_EFFECT.NO_EFFECT)
                        or row["result_event_id"] is None)

            def dispatched_result(row):
                result = row["result_json"]
                return result is not None and result["settlement"]["basis"] != "not_dispatched"

            unresolved += int(unresolved_result(final)) - int(unresolved_result(original))
            dispatched += int(dispatched_result(final)) - int(dispatched_result(original))
    with closing(connection.execute(
        "SELECT 1 FROM operation_runs WHERE action_id = ? AND kind = ?"
        " AND query_purpose = ? AND status IN (?, ?) LIMIT 1",
        (action["id"], int(_KIND.QUERY_ACTIVITY), int(_PURPOSE.START_CONFIRMATION),
         int(_RUN.PENDING), int(_RUN.ACTIVE)),
    )) as cursor:
        pending_confirmation = cursor.fetchone() is not None
    attempts = ()
    if is_hasselblad_photo(action) and used:
        with closing(connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no", (run["id"],)
        )) as cursor:
            ids = [row[0] for row in cursor.fetchall()]
        attempts = [after("operation_attempts", row_facts(connection, "operation_attempts", identity)) for identity in ids]
    return start_facts_from_summary(action, activity, run,
        (count, used, unresolved, dispatched), pending_confirmation, attempts)


def start_facts_from_summary(action, activity, run, statistics, pending_confirmation, entry_attempts=()):
    """SQL 聚合和完整原行共用的 START 分区；不推断会话许可。"""
    count, used, unresolved, dispatched = statistics
    if action["execution_started"] and activity is None:
        raise ConsistencyError(f"已执行拍摄缺少设备活动: {action['id']}")
    if run is not None and (activity is None or run["kind"] != int(_KIND.START)
            or run["action_id"] != action["id"] or run["activity_id"] != activity["id"]
            or count != used or used != run["attempts_used"]):
        raise ConsistencyError(f"启动责任或累计次数与原事实不符: {action['id']}")
    dispatch = None if activity is None else int(_DISPATCH(activity["dispatch_state"]))
    if activity is None:
        return StartFacts(None, run, used, run is None and not pending_confirmation, None)
    no_start_observation = activity["activity_state"] == 1 and activity["started_at"] is None
    if used == 0:
        not_started = dispatch == int(_DISPATCH.NOT_DISPATCHED) and no_start_observation
    else:
        not_started = (unresolved == 0 and no_start_observation
                       and dispatch != int(_DISPATCH.SUCCESS_RETURNED))
        if not_started and dispatch == int(_DISPATCH.MAY_HAVE_DISPATCHED):
            dispatch = int(_DISPATCH.NOT_DISPATCHED if dispatched == 0
                           else _DISPATCH.REJECTED_WITHOUT_EFFECT)
    if is_hasselblad_photo(action) and used:
        not_started = entry_from_rows(action, activity, entry_attempts) is EntryPhase.BLOCKED
    return StartFacts(activity, run, used, not_started and not pending_confirmation, dispatch)


def start_facts_from_rows(action, activities, runs, attempts_by_run, *, activity_targets=()):
    """自身活动范围与 run 引用目标分别输入；完整 run/attempt 生成有限统计。"""
    if len(activities) > 1 or any(row["action_id"] != action["id"] for row in activities):
        raise ConsistencyError("原拍摄活动归属或唯一性不符")
    if set(attempts_by_run) != {run["id"] for run in runs}:
        raise ConsistencyError("原操作与完整尝试范围不一致")
    targets = {}
    for activity in (*activities, *activity_targets):
        if activity["id"] in targets and targets[activity["id"]] != activity:
            raise ConsistencyError("原操作目标活动事实矛盾")
        targets[activity["id"]] = activity
    state = {"device_activities": targets,
             "operation_runs": {run["id"]: run for run in runs}}
    starts, pending_confirmation = [], False
    for run in runs:
        if run["action_id"] != action["id"] or run["id"] not in attempts_by_run:
            raise ConsistencyError("原操作责任缺少归属或完整尝试范围")
        _verify_start_scope_run(run, state)
        attempts = attempts_by_run[run["id"]]
        numbers = {attempt["attempt_no"] for attempt in attempts}
        if (any(attempt["run_id"] != run["id"] for attempt in attempts)
                or len(numbers) != len(attempts) or len(attempts) != run["attempts_used"]
                or len(attempts) != max(numbers, default=0)
                or (numbers and min(numbers) != 1)):
            raise ConsistencyError("原操作尝试的归属或连续累计次数不符")
        if run["responsibility_key"] == f"start/{action['id']}":
            starts.append(run)
        if (run["kind"] == int(_KIND.QUERY_ACTIVITY)
                and run["query_purpose"] == int(_PURPOSE.START_CONFIRMATION)
                and run["status"] in (int(_RUN.PENDING), int(_RUN.ACTIVE))):
            pending_confirmation = True
    if len(starts) > 1:
        raise ConsistencyError("原 START 责任不唯一")
    run = starts[0] if starts else None
    attempts = () if run is None else attempts_by_run[run["id"]]
    unresolved = sum(attempt["status"] == int(_ATTEMPT.RUNNING)
        or attempt["effect_state"] != int(_EFFECT.NO_EFFECT)
        or attempt["result_event_id"] is None for attempt in attempts)
    dispatched = sum(attempt["result_json"] is not None
        and attempt["result_json"]["settlement"]["basis"] != "not_dispatched" for attempt in attempts)
    return start_facts_from_summary(action, activities[0] if activities else None, run,
        (len(attempts), max((a["attempt_no"] for a in attempts), default=0), unresolved, dispatched),
        pending_confirmation, attempts)


def _verify_start_scope_run(run, state):
    """活动目标沿原操作职责校验；其他目标只核原固定组合与责任键。"""
    from camctl.persistence.repositories.operations import _verify_run_identity, _fixed_reference
    if run["kind"] == int(_KIND.EMERGENCY_STOP):
        activity = _fixed_reference(state, "device_activities", run["activity_id"])
        if activity["action_id"] != run["action_id"]:
            raise ConsistencyError("原应急补记与目标活动所属动作不符")
        return
    if run["activity_id"] is not None or (run["copy_id"] is None and run["cleanup_item_id"] is None and run["obtain_item_id"] is None):
        _verify_run_identity(run, state)
        return
    from camctl.operations.attempts import OperationKind, QueryPurpose, AttemptTarget, operation_responsibility_key
    try:
        kind = OperationKind[_KIND(run["kind"]).name]
        purpose = None if run["query_purpose"] is None else QueryPurpose[_PURPOSE(run["query_purpose"]).name]
        expected = operation_responsibility_key(kind, run["action_id"], AttemptTarget(
            activity_id=run["activity_id"], copy_id=run["copy_id"], cleanup_item_id=run["cleanup_item_id"], obtain_item_id=run["obtain_item_id"]), purpose)
    except (KeyError, ValueError) as error:
        raise ConsistencyError("原操作固定身份组合无效") from error
    if run["responsibility_key"] != expected:
        raise ConsistencyError("原操作责任键与固定身份不符")


def release_basis_holds(facts: Mapping[str, Any]) -> bool:
    """活动结束、可靠未派发、无效果或适用完成依据允许解除占用。"""
    return (facts.get("activity_state") == 3
            or facts.get("dispatch_state") in (1, 4)
            or facts.get("completion_basis") == 3)


def output_scope_resolved(facts: Mapping[str, Any], recovery=None) -> bool:
    """基准已固定，且原集合结论或独立集合事实证明范围确定。"""
    ordinary = (facts.get("baseline_state") == 3
            and (facts.get("result_set_state") == 3
                 or facts.get("output_set_finalized_event_id") is not None))
    if ordinary:
        return True
    if not isinstance(recovery, Mapping):
        return False
    from camctl.capture.software_recovery import RecoveryPhase
    return (facts.get("baseline_state") == 3 and facts.get("activity_state") == 3
        and recovery.get("phase") == RecoveryPhase.READY.value
        and recovery.get("prepared") is True and recovery.get("scope_finalized") is True
        and recovery.get("cursor") is None
        and recovery.get("scope_pages", 0) > 0
        and recovery.get("scope_first_event_id") is not None and recovery.get("scope_last_event_id") is not None
        and recovery.get("boot_before") is not None and recovery.get("boot_after") is not None
        and recovery["boot_before"] != recovery["boot_after"]
        and recovery.get("baseline_first") == facts.get("baseline_first_event_id")
        and recovery.get("baseline_last") == facts.get("baseline_last_event_id"))


def occupancy_release_allowed(facts: Mapping[str, Any], *, unstarted: bool = False,
                              preparation_resolved: bool = False) -> bool:
    """释放依据与输出范围限制都满足，且占用尚未释放。"""
    return (facts["occupancy_state"] == 1 and release_basis_holds(facts)
            and (facts["ownership_mode"] != 2 or (unstarted and preparation_resolved)
                 or output_scope_resolved(facts)))


def start_finish_event(run: Mapping[str, Any] | None, status: int, occurred_at: int):
    """构造适用 START 结束事件；不存在或已终态时不创建、不改写。"""
    if run is None or run["status"] not in (int(_RUN.PENDING), int(_RUN.ACTIVE)):
        return None
    if status not in (int(_RUN.CANCELED), int(_RUN.EXPIRED)):
        raise ValueError("本地启动收场只接受取消或过期")
    return event_envelope(0, 0, 10, 3, (update_change(
        "operation_runs", run["id"],
        {"status": run["status"], "retry_wait_required": run["retry_wait_required"],
         "error_json": run["error_json"]},
        {"status": status, "retry_wait_required": 0, "error_json": None}),), occurred_at)


def unstarted_events(facts: StartFacts, occurred_at: int, *,
                     run_status: int | None, release: bool = True,
                     preparation_resolved: bool = False):
    """把已证明无启动效果的派发结论、责任结束和适用释放组合成事件。"""
    if not facts.not_started:
        raise ConsistencyError("本地终止缺少可靠未启动依据")
    events = []
    activity = facts.activity
    if activity is not None and activity["dispatch_state"] != facts.dispatch_state:
        events.append(event_envelope(0, 0, 13, 2, (update_change(
            "device_activities", activity["id"],
            {"dispatch_state": activity["dispatch_state"]},
            {"dispatch_state": facts.dispatch_state}),), occurred_at))
    if run_status is not None:
        finish = start_finish_event(facts.run, run_status, occurred_at)
        if finish is not None:
            events.append(finish)
    if activity is not None and release and occupancy_release_allowed(
            {**activity, "dispatch_state": facts.dispatch_state}, unstarted=True,
            preparation_resolved=preparation_resolved or activity["baseline_state"] == 3):
        events.append(event_envelope(0, 0, 13, 3, (update_change(
            "device_activities", activity["id"],
            {"occupancy_state": 1}, {"occupancy_state": 2}),), occurred_at,
            evidence={"preparation_resolved": True} if preparation_resolved else {}))
    return events


def include_start_facts(facts: StartFacts, state, owners) -> None:
    """将本地终止涉及的原行和历史归属加入同一事务上下文。"""
    for table, row in (("device_activities", facts.activity), ("operation_runs", facts.run)):
        if row is not None:
            state.setdefault(table, {})[row["id"]] = row
            owners[(table, row["id"])] = ("action", row["action_id"])


def verify_unstarted_final(connection, action, saved=(), *, released=True,
                           preparation_error=None) -> None:
    """核实本地终止的当前可靠结果与新增事件的身份、字段和终态。"""
    facts = load_start_facts(connection, action)
    if not facts.not_started:
        raise ConsistencyError("本地终止的原未启动依据不可靠")
    if facts.run is not None and facts.run["status"] in (int(_RUN.PENDING), int(_RUN.ACTIVE)):
        raise ConsistencyError("本地终止仍有普通启动责任")
    preparation_resolved = (preparation_error is not None or any(
        event["body"]["evidence"].get("preparation_resolved") is True for event in saved))
    if released and facts.activity is not None and occupancy_release_allowed(facts.activity, unstarted=True,
            preparation_resolved=preparation_resolved or facts.activity["baseline_state"] == 3):
        raise ConsistencyError("本地终止缺少适用的占用释放")
    for event in saved:
        kind = event["type"], event["reason"]
        if kind not in ((13, 2), (13, 3), (10, 3)):
            continue
        rows = event["body"]["rows"]
        if len(rows) != 1:
            raise ConsistencyError("本地终止的伴随事件行数不符")
        row = rows[0]
        original = facts.run if kind == (10, 3) else facts.activity
        table = "operation_runs" if kind == (10, 3) else "device_activities"
        columns = ({"status", "retry_wait_required", "error_json"} if kind == (10, 3)
                   else {"last_error_json"} if kind == (13, 2) and preparation_error is not None
                   else {"dispatch_state"} if kind == (13, 2) else {"occupancy_state"})
        if (original is None or row["table"] != table or row["id"] != original["id"]
                or set(row["after"]["values"]) - columns
                or (kind == (10, 3) and "status" not in row["after"]["values"])
                or (kind != (10, 3) and set(row["after"]["values"]) != columns)):
            raise ConsistencyError("本地终止的伴随事实与原责任不符")
        if any(original[column] != value for column, value in row["after"]["values"].items()):
            raise ConsistencyError("本地终止的伴随结果与保存事实不符")
