"""拍摄触发的设备检查创建与原连接独立核验。"""

from contextlib import closing
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, UtcMicros
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.devices.start_entry import is_hasselblad_photo
from camctl.history.events import load_event_registry
from camctl.history.validators import EventContext, register_guard
from camctl.persistence.transaction import (BoundedCommandPlan, CommandPlan, commit_operation,
    event_envelope, next_row_id, row_change, row_facts, read_saved_transaction_page,
    read_transaction_range)
from .availability_target import (AvailabilityCreationTargets, AvailabilityTargetBlocked,
    AvailabilityTargetIdentityMismatch, AvailabilityTargetNotRequired, AvailabilityTargetReuse,
    iter_creation_targets)


@dataclass(frozen=True)
class CreateAvailabilityCheck:
    action_id: int
    identity: CameraIdentity
    trusted_wall_now: int
    prepare_lead_s: Decimal
    occurred_at: int
    batch_size: int = 128

    def __post_init__(self):
        ObjectId(self.action_id)
        UtcMicros(self.trusted_wall_now)
        UtcMicros(self.occurred_at)
        if (not isinstance(self.identity, CameraIdentity) or not isinstance(self.prepare_lead_s, Decimal)
                or not self.prepare_lead_s.is_finite() or self.prepare_lead_s < 0
                or type(self.batch_size) is not int or not 1 <= self.batch_size <= 128):
            raise ValueError("检查创建申请的身份、提前量或批次无效")


class CreateDisposition(Enum):
    CREATED = "created"
    REUSED = "reused"
    WAIT = "wait"
    NOT_REQUIRED = "not_required"
    IDENTITY_MISMATCH = "identity_mismatch"


@dataclass(frozen=True)
class CreateResult:
    disposition: CreateDisposition
    check_id: int | None = None
    run_id: int | None = None


def _one(connection, sql, args=()):
    with closing(connection.execute(sql, args)) as cursor:
        return cursor.fetchone()


def _request_document(request):
    return {"action_id": request.action_id, "identity": {"serial": request.identity.serial,
        "product_id": request.identity.product_id}, "trusted_wall_now": request.trusted_wall_now,
        "prepare_lead_s": request.prepare_lead_s, "occurred_at": request.occurred_at,
        "batch_size": request.batch_size}


def _request(raw):
    if not isinstance(raw, dict) or set(raw) != {"action_id", "identity", "trusted_wall_now",
            "prepare_lead_s", "occurred_at", "batch_size"}:
        raise ConsistencyError("检查创建缺少完整申请")
    try:
        return CreateAvailabilityCheck(raw["action_id"], CameraIdentity(**raw["identity"]),
            raw["trusted_wall_now"], Decimal(raw["prepare_lead_s"]), raw["occurred_at"], raw["batch_size"])
    except (TypeError, ValueError, KeyError) as error:
        raise ConsistencyError("检查创建申请无效") from error


def _qualify(connection, request, boundary):
    action = row_facts(connection, "actions", request.action_id)
    if (action is None or not is_hasselblad_photo(action) or action["status"] != int(enum_for("actions.status").PENDING)
            or action["execution_started"] != 0 or action["cancel_requested"] != 0):
        raise AvailabilityTargetBlocked("触发拍摄不再具备准备资格")
    if not (Decimal(action["scheduled_at"]) - request.prepare_lead_s * 1_000_000
            <= request.trusted_wall_now <= action["scheduled_at"] + action["max_delay_ms"] * 1000):
        raise AvailabilityTargetBlocked("触发拍摄不在允许的准备时间")
    if _one(connection, "SELECT 1 FROM device_activities WHERE action_id=?", (action["id"],)):
        raise AvailabilityTargetBlocked("触发拍摄已有活动，沿原责任处理")
    if _one(connection, "SELECT 1 FROM operation_runs WHERE action_id=? AND kind=? LIMIT 1",
            (action["id"], int(enum_for("operation_runs.kind").START))):
        raise AvailabilityTargetBlocked("触发拍摄已有 START 责任")
    from camctl.devices.capture_preparation import CAPTURE_ACTION_TYPES
    after = 0
    while True:
        with closing(connection.execute(
            "SELECT id,scheduled_at,plan_id,input_index,max_delay_ms FROM actions"
            " WHERE device_id=? AND id>? AND status=1 AND execution_started=0 AND cancel_requested=0"
            " ORDER BY id LIMIT 128", (action["device_id"], after),
        )) as cursor:
            page = tuple(cursor.fetchmany(128))
        if not page:
            break
        for other_id, scheduled, plan_id, index, delay in page:
            other = row_facts(connection, "actions", other_id)
            if (other["type"] in CAPTURE_ACTION_TYPES and other_id != action["id"]
                    and (scheduled, plan_id, index) < (action["scheduled_at"], action["plan_id"], action["input_index"])
                    and Decimal(scheduled) - request.prepare_lead_s * 1_000_000
                    <= request.trusted_wall_now <= scheduled + delay * 1000):
                raise AvailabilityTargetBlocked("存在更早的合格拍摄候选")
        after = page[-1][0]
        if len(page) < 128:
            break
    if _one(connection, "SELECT 1 FROM file_copies WHERE slot_device_id=? LIMIT 1", (action["device_id"],)):
        raise AvailabilityTargetBlocked("设备读取机会尚未收场")
    from camctl.scheduling.resources import device_read_in_flight, device_start_in_flight
    if device_read_in_flight(connection, action["device_id"]) or device_start_in_flight(connection, action["device_id"]):
        raise AvailabilityTargetBlocked("设备调用尚未收场")
    running = int(enum_for("operation_attempts.status").RUNNING)
    if (_one(connection, "SELECT 1 FROM device_activities d JOIN operation_runs r ON r.activity_id=d.id"
             " JOIN operation_attempts t ON t.run_id=r.id WHERE d.resource_key=? AND t.status=? LIMIT 1",
             (request.identity.resource_key, running))
            or _one(connection, "SELECT 1 FROM actions a JOIN device_activities d ON d.action_id=a.id"
                " JOIN baseline_read_calls b ON b.activity_id=d.id WHERE a.device_id=? AND b.state=1 LIMIT 1",
                (action["device_id"],))):
        raise AvailabilityTargetBlocked("原设备调用或目录读取尚未收场")
    from .physical_alias import physical_alias_conflict
    if physical_alias_conflict(connection, action["device_id"], request.identity):
        raise AvailabilityTargetBlocked("其他逻辑设备仍持有该机身责任")
    if _one(connection, "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " JOIN device_availability_checks c ON c.id=r.availability_check_id"
            " WHERE c.resource_key=? AND t.status=? LIMIT 1", (request.identity.resource_key, running)):
        raise AvailabilityTargetBlocked("设备检查尝试尚未收场")
    check = {"action_id": action["id"], "trigger_copy_id": None, "device_id": action["device_id"],
        "driver_id": action["driver_id"], "expected_identity_json": {"serial": request.identity.serial,
        "product_id": request.identity.product_id}, "resource_key": request.identity.resource_key,
        "rule_version": 1, "target_boundary_event_id": boundary}
    count = sum(len(page) for page in iter_creation_targets(connection, check, batch_size=request.batch_size))
    return action, check, count


def _head(connection, request, boundary):
    action, check, count = _qualify(connection, request, boundary)
    check_id, run_id = next_row_id(connection, "device_availability_checks"), next_row_id(connection, "operation_runs")
    target_id = next_row_id(connection, "device_availability_targets")
    run = {"action_id": request.action_id, "delivery_id": None,
        "kind": int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY), "query_purpose": None,
        "responsibility_key": f"availability/{check_id}", "activity_id": None, "copy_id": None,
        "cleanup_item_id": None, "obtain_item_id": None, "availability_check_id": check_id,
        "session_key": None, "status": int(enum_for("operation_runs.status").PENDING), "attempts_used": 0,
        "max_attempts_used": None, "timeout_s_json": None, "retry_interval_s_json": None,
        "retry_wait_required": 0, "error_json": None}
    return action, {"id": check_id, **check}, run_id, run, target_id, count


def _events(connection, request, head, transaction):
    _, check, run_id, run, target_id, count = head
    event = load_event_registry()["events"]["AVAILABILITY_CHECK"]
    reason = event["branches"]["CREATE"]["reason"]
    identity = transaction.first_event_id
    yield event_envelope(identity, transaction.txn_id, event["id"], reason,
        (row_change("device_availability_checks", check["id"], {k: v for k, v in check.items() if k != "id"}),
         row_change("operation_runs", run_id, run)), request.occurred_at,
        {"availability_create_request": _request_document(request)})
    identity += 1
    seen = 0
    for page in iter_creation_targets(connection, check, batch_size=request.batch_size):
        rows = []
        for activity_id in page:
            rows.append(row_change("device_availability_targets", target_id,
                {"check_id": check["id"], "action_id": request.action_id, "activity_id": activity_id}))
            target_id += 1
            seen += 1
        yield event_envelope(identity, transaction.txn_id, event["id"], reason, rows, request.occurred_at)
        identity += 1
    if seen != count or identity != transaction.last_event_id + 1:
        raise ConsistencyError("检查目标与原事务范围不一致")


def _context(connection, event, transaction, action_id):
    action = row_facts(connection, "actions", action_id)
    state = {"actions": {action_id: action}}
    owners = {(row.table, row.row_id): ("action", action_id) for row in event.rows}
    for row in event.rows:
        current = row_facts(connection, row.table, row.row_id)
        if current is not None:
            state.setdefault(row.table, {})[row.row_id] = current
    return EventContext(transaction, owners, state)


def _reuse(connection, request, key, transaction):
    """按原键分页核对完整创建历史和不可变目标投影。"""
    from camctl.history.validators import validate_event_structure
    cursor = None
    header = None
    seen = 0
    last_target_id = 0
    while True:
        page = read_saved_transaction_page(connection, key, transaction, cursor, limit=64)
        for event in page.items:
            if (validate_event_structure(event)[:2] != ("AVAILABILITY_CHECK", "CREATE")
                    or event.occurred_at != request.occurred_at):
                raise ConsistencyError("原键不是完整的设备检查创建事务")
            if header is None:
                if not json_equal(event.evidence.get("availability_create_request"), _request_document(request)):
                    raise ConsistencyError("原检查创建键与申请不同")
                checks = [row for row in event.rows if row.table == "device_availability_checks"]
                runs = [row for row in event.rows if row.table == "operation_runs"]
                if len(event.rows) != 2 or len(checks) != 1 or len(runs) != 1:
                    raise ConsistencyError("原检查创建事务缺少唯一责任头")
                check, run = checks[0], runs[0]
                current_check = row_facts(connection, "device_availability_checks", check.row_id)
                current_run = row_facts(connection, "operation_runs", run.row_id)
                if (current_check is None or current_run is None
                        or any(not json_equal(current_check.get(k), v) for k, v in check.after.values.items())
                        or current_check["action_id"] != request.action_id
                        or current_check["expected_identity_json"] != {"serial": request.identity.serial,
                            "product_id": request.identity.product_id}
                        or current_check["resource_key"] != request.identity.resource_key
                        or current_check["target_boundary_event_id"] != transaction.first_event_id - 1
                        or current_run["action_id"] != request.action_id
                        or current_run["kind"] != int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY)
                        or current_run["availability_check_id"] != check.row_id
                        or current_run["responsibility_key"] != f"availability/{check.row_id}"):
                    raise ConsistencyError("原检查投影与创建历史不一致")
                header = (check.row_id, run.row_id)
                continue
            if event.evidence or not event.rows or len(event.rows) > request.batch_size:
                raise ConsistencyError("原检查目标页结构无效")
            for row in event.rows:
                target = row_facts(connection, "device_availability_targets", row.row_id)
                if (row.table != "device_availability_targets" or row.row_id <= last_target_id
                        or target is None or any(not json_equal(target.get(k), v) for k, v in row.after.values.items())
                        or target["check_id"] != header[0] or target["action_id"] != request.action_id):
                    raise ConsistencyError("原目标历史与不可变投影不一致")
                last_target_id = row.row_id
                seen += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    count = _one(connection, "SELECT COUNT(*) FROM device_availability_targets WHERE check_id=?", (header[0],))[0]
    if seen == 0 or count != seen:
        raise ConsistencyError("原检查目标历史不完整")
    return CreateResult(CreateDisposition.CREATED, *header)


class CreateCommand:
    def __init__(self, request, key):
        self.request, self.key = request, OperationKey(key)

    def plan(self, scope):
        db = scope.connection
        saved = _one(db, "SELECT id FROM history_transactions WHERE operation_key=?", (str(self.key),))
        if saved is not None:
            transaction = read_transaction_range(db, saved[0])
            return CommandPlan((), {}, {}, _reuse(db, self.request, self.key, transaction), read_only=True)
        try:
            head = _head(db, self.request, scope.max_event_id)
        except AvailabilityTargetReuse as existing:
            found = _one(db, "SELECT id FROM operation_runs WHERE availability_check_id=? AND kind=?",
                (existing.check_id, int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY)))
            if found is None:
                raise ConsistencyError("已有检查缺少原操作责任")
            return CommandPlan((), {}, {}, CreateResult(CreateDisposition.REUSED, existing.check_id, found[0]), read_only=True)
        except (AvailabilityTargetBlocked, AvailabilityTargetNotRequired, AvailabilityTargetIdentityMismatch) as error:
            disposition = {AvailabilityTargetBlocked: CreateDisposition.WAIT,
                AvailabilityTargetNotRequired: CreateDisposition.NOT_REQUIRED,
                AvailabilityTargetIdentityMismatch: CreateDisposition.IDENTITY_MISMATCH}[type(error)]
            return CommandPlan((), {}, {}, CreateResult(disposition), read_only=True)
        allocation = scope.allocate(1 + (head[-1] + self.request.batch_size - 1) // self.request.batch_size)
        from camctl.contracts.history_values import TransactionRange
        transaction = TransactionRange(allocation.txn_id, allocation.first_event_id, allocation.last_event_id)
        return BoundedCommandPlan(_events(db, self.request, head, transaction),
            lambda event, tx: _context(db, event, tx, self.request.action_id),
            CreateResult(CreateDisposition.CREATED, head[1]["id"], head[2]), lambda connection: None)


def ensure_check(request, key, owned):
    from .operations import _outcome_of
    return _outcome_of(commit_operation(CreateCommand(request, key), key, owned))


class CreationGroup:
    """由事务内核在原连接独立重算头和目标，逐事件封口。"""

    def __init__(self, connection, event, transaction):
        if event.event_id != transaction.first_event_id:
            raise ConsistencyError("检查创建必须从事务首事件开始")
        self.connection, self.transaction = connection, transaction
        self.request = _request(event.evidence.get("availability_create_request"))
        self.head = _head(connection, self.request, transaction.first_event_id - 1)
        self.expected = iter(_events(connection, self.request, self.head, transaction))
        self.targets = AvailabilityCreationTargets(connection, self.head[1], transaction,
            batch_size=self.request.batch_size)
        self.position = 0
        self.pending = None

    def bind(self, event, context):
        expected = next(self.expected, None)
        if (self.pending is not None or expected is None
                or replace(event, change_seq=None) != expected
                or context != _context(self.connection, event, self.transaction, self.request.action_id)):
            raise ConsistencyError("检查创建事件与原连接独立计算的事件不同")
        if self.position:
            self.targets.prepare(event)
        self.pending = replace(event, change_seq=None)

    def seal(self, validated):
        if self.pending != replace(validated.envelope, change_seq=None):
            raise ConsistencyError("检查创建事件未通过原事务校验")
        if self.position:
            self.targets.seal(validated)
        self.pending = None
        self.position += 1

    def finish(self):
        if self.pending is not None or next(self.expected, None) is not None:
            raise ConsistencyError("检查创建事务未完整封口")
        self.targets.finish()


def availability_guard(event, context):
    group = context.availability_reads
    if not isinstance(group, CreationGroup) or group.pending != replace(event, change_seq=None):
        raise ConsistencyError("设备检查事件缺少原内核独立事务组")


def register_availability_guard():
    register_guard("availability_check", availability_guard)
