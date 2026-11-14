"""物化设备检查事实，事务连接和分页游标留在存储层。"""

from contextlib import closing
from typing import Any, NamedTuple, TypedDict, cast

from camctl.contracts.values import ConsistencyError, ObjectId


class OpenAvailabilityCheck(NamedTuple):
    check_id: int
    run_id: int
    trigger_copy_id: int | None


class AvailabilityCheckPage(NamedTuple):
    entries: tuple[tuple[int, int, int | None], ...]
    next_after: int | None


class AvailabilityRunFacts(TypedDict):
    id: int
    status: int
    error_json: dict | None
    attempts_used: int
    max_attempts_used: int | None
    retry_wait_required: int
    responsibility_key: str
    last_attempt_id: int | None
    running_attempt: int | None
    intent_event_id: int | None


class AvailabilityActionFacts(TypedDict):
    status: int
    cancel_requested: int
    scheduled_at: int
    max_delay_ms: int


def read_run_facts(owned, run_id) -> AvailabilityRunFacts:
    from camctl.persistence.repositories.transactions import row_facts
    run = row_facts(owned.connection, "operation_runs", run_id)
    if run is None:
        raise ConsistencyError("设备检查流程不存在")
    row = owned.connection.execute(
        "SELECT id,status,result_event_id,intent_event_id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
        (run_id,)).fetchone()
    run["last_attempt_id"] = None if row is None else row[0]
    run["running_attempt"] = None if row is None or row[1] != 1 and row[2] is not None else row[0]
    run["intent_event_id"] = None if row is None else row[3]
    return cast(AvailabilityRunFacts, run)


def read_open_check(owned, request):
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    from camctl.persistence.repositories.transactions import row_facts
    rows = owned.connection.execute(
        "SELECT c.id,r.id,c.trigger_copy_id FROM device_availability_checks c"
        " JOIN operation_runs r ON r.availability_check_id=c.id"
        " WHERE c.action_id=? AND c.resource_key=? AND r.status IN (1,2) ORDER BY c.id LIMIT 2",
        (request.action_id, request.identity.resource_key),
    ).fetchall()
    if len(rows) > 1:
        raise ConsistencyError("同一动作与机身存在多个开放检查")
    if not rows:
        return None
    check = row_facts(owned.connection, "device_availability_checks", rows[0][0])
    try:
        if check is None:
            raise ValueError("缺少固定检查")
        fixed = CameraIdentity(**check["expected_identity_json"])
        if check["resource_key"] != fixed.resource_key:
            raise ValueError("固定检查资源键不符")
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("开放检查缺少可靠固定机身身份") from error
    return ("identity_mismatch",) if fixed != request.identity else OpenAvailabilityCheck(*rows[0])


def read_binding_checks_page(owned, request, *, after=0):
    """读取一页当前仍需消费的检查，保留原始页的继续位置。"""
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    from camctl.persistence.repositories.transactions import row_facts
    if request.trigger_copy_id is None:
        condition, params = "c.trigger_copy_id IS NULL", (request.action_id,)
    else:
        condition, params = "c.trigger_copy_id IS NOT NULL AND c.resource_key=?", (request.action_id, request.identity.resource_key)
    if after != 0:
        ObjectId(after)
    matching = []
    with closing(owned.connection.execute(
        "SELECT c.id,r.id,c.trigger_copy_id,r.status FROM device_availability_checks c"
        " JOIN operation_runs r ON r.availability_check_id=c.id"
        " WHERE c.action_id=? AND " + condition + " AND r.status IN (1,2,4,6) AND c.id>? ORDER BY c.id LIMIT 128",
        (*params, after),
    )) as cursor:
        rows = cursor.fetchall()
    ids = tuple(ObjectId(row[0]) for row in rows)
    if any(left >= right for left, right in zip((after, *ids), ids)):
        raise ConsistencyError("检查读取页不是严格递增的唯一身份")
    for check_id, run_id, copy_id, status in rows:
        check = row_facts(owned.connection, "device_availability_checks", check_id)
        try:
            if check is None:
                raise ValueError("缺少固定检查")
            fixed = CameraIdentity(**check["expected_identity_json"])
            if check["resource_key"] != fixed.resource_key:
                raise ValueError("固定检查资源键不符")
        except (KeyError, TypeError, ValueError) as error:
            raise ConsistencyError("检查缺少可靠固定机身身份") from error
        if request.trigger_copy_id is not None and fixed != request.identity:
            continue
        if status in (4, 6):
            run = row_facts(owned.connection, "operation_runs", run_id)
            if run is None:
                raise ConsistencyError("终态检查缺少原流程")
            error = run.get("error_json")
            if not isinstance(error, dict) or error.get("code") != "device_binding_unavailable":
                from camctl.persistence.repositories.availability_failure import failure_basis
                if failure_basis(owned.connection, check_id, request.action_id) is None:
                    continue
        matching.append((check_id, run_id, copy_id))
    return AvailabilityCheckPage(tuple(matching), rows[-1][0] if len(rows) == 128 else None)


def read_action_facts(owned, action_id) -> AvailabilityActionFacts:
    from camctl.persistence.repositories.transactions import row_facts
    action = row_facts(owned.connection, "actions", action_id)
    if action is None:
        raise ConsistencyError("设备检查触发动作不存在")
    return cast(AvailabilityActionFacts, action)


def read_dispatch_decision(owned, ticket: Any, request: Any):
    from camctl.persistence.repositories.availability_intent import recheck_availability_dispatch
    return recheck_availability_dispatch(owned.connection, ticket, request)
