"""可用性检查目标的固定活动事实。"""

from collections.abc import Mapping
from contextlib import closing
from copy import deepcopy

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ObjectId
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.devices.start_entry import is_hasselblad_photo


class AvailabilityTargetBlocked(ValueError):
    """旧责任尚未收场或不适用于检查，调用方应等待原责任。"""


class AvailabilityTargetReuse(ValueError):
    """触发动作已经为目标建立检查，应复用原责任。"""

    def __init__(self, check_id: int):
        self.check_id = ObjectId(check_id)
        super().__init__(f"触发动作已有检查责任: {check_id}")


class AvailabilityTargetNotRequired(ValueError):
    """固定资源当前没有需要检查的旧活动。"""


class AvailabilityTargetIdentityMismatch(ValueError):
    """原逻辑设备的固定机身与当前检查身份不同。"""


class AvailabilityTargetStateError(ValueError):
    """保存的必要事实缺失或相互矛盾。"""


class AvailabilityCreationTargets:
    """独立消费原连接目标，验证已经固定的创建头之后的全部目标页。"""

    def __init__(self, connection, check: Mapping, transaction, *, batch_size: int = 128):
        if type(batch_size) is not int or not 1 <= batch_size <= 128:
            raise ValueError("活动读取每批必须为 1 至 128 项")
        fixed = deepcopy(check)
        self._check_id = ObjectId(fixed["id"])
        self._action_id = ObjectId(fixed["action_id"])
        self._pages = iter(iter_creation_targets(connection, fixed, batch_size=batch_size))
        self._transaction_id = ObjectId(transaction.txn_id)
        self._next_event_id = ObjectId(transaction.first_event_id) + 1
        self._last_event_id = ObjectId(transaction.last_event_id)
        self._batch_size = batch_size
        self._pending = None
        self._count = 0
        self._finished = False

    def prepare(self, event) -> None:
        """只接受下一页，不把提案自身当作完整目标依据。"""
        if (self._finished or self._pending is not None or event.transaction_id != self._transaction_id
                or event.event_id != self._next_event_id or event.event_id > self._last_event_id
                or not 1 <= len(event.rows) <= self._batch_size):
            raise AvailabilityTargetStateError("检查目标页次序或大小无效")
        expected = next(self._pages, None)
        if expected is None or len(event.rows) != len(expected):
            raise AvailabilityTargetStateError("检查目标页与原连接事实数量不同")
        for row, activity_id in zip(event.rows, expected):
            values = row.after.values
            if (row.table != "device_availability_targets" or row.before.exists or not row.after.exists
                    or set(values) != {"check_id", "action_id", "activity_id"}
                    or any(isinstance(values[name], bool) or not isinstance(values[name], int) for name in values)
                    or values["check_id"] != self._check_id or values["action_id"] != self._action_id
                    or values["activity_id"] != activity_id):
                raise AvailabilityTargetStateError("检查目标页与原连接事实不一致")
        self._pending = (event.event_id, event.transaction_id, event.rows)

    def seal(self, validated) -> None:
        """仅在该页通过事件校验之后推进封口。"""
        event = validated.envelope
        if self._pending != (event.event_id, event.transaction_id, event.rows):
            raise AvailabilityTargetStateError("检查目标页没有通过原事件校验")
        self._pending = None
        self._count += 1
        self._next_event_id += 1

    def finish(self) -> None:
        if (self._finished or self._pending is not None or self._count == 0
                or self._next_event_id != self._last_event_id + 1 or next(self._pages, None) is not None):
            raise AvailabilityTargetStateError("检查目标事务没有完整覆盖原连接目标")
        self._finished = True


def iter_held_activity_ids(connection, resource_key: str, *, batch_size: int = 128):
    """按固定机身资源键有界枚举 HELD 活动；读取期间须保持原写事务。"""
    if not isinstance(resource_key, str) or not resource_key:
        raise ValueError("设备资源键必须是非空字符串")
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise ValueError("活动读取每批必须为 1 至 128 项")
    held = int(enum_for("device_activities.occupancy_state").HELD)
    after = 0
    while True:
        with closing(connection.execute(
            "SELECT id FROM device_activities WHERE resource_key=? AND occupancy_state=?"
            " AND id>? ORDER BY id LIMIT ?", (resource_key, held, after, batch_size),
        )) as cursor:
            page = tuple(cursor.fetchmany(batch_size))
        if not page:
            return
        ids = tuple(ObjectId(row[0]) for row in page)
        if any(left >= right for left, right in zip((after, *ids), ids)):
            raise ValueError("活动读取页不是严格递增的有效身份")
        yield ids
        after = ids[-1]
        if len(page) < batch_size:
            return


def iter_logical_held_activities(connection, device_id: str, *, batch_size: int = 128):
    """按原动作身份有界枚举逻辑设备的 HELD 活动，包括终态动作。"""
    if not isinstance(device_id, str) or not device_id:
        raise ValueError("逻辑设备身份必须是非空字符串")
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise ValueError("活动读取每批必须为 1 至 128 项")
    held = int(enum_for("device_activities.occupancy_state").HELD)
    after = 0
    while True:
        with closing(connection.execute(
            "SELECT a.id,d.id FROM actions a JOIN device_activities d ON d.action_id=a.id"
            " WHERE a.device_id=? AND a.id>? AND d.occupancy_state=? ORDER BY a.id LIMIT ?",
            (device_id, after, held, batch_size),
        )) as cursor:
            page = tuple(cursor.fetchmany(batch_size))
        if not page:
            return
        pairs = tuple((ObjectId(row[0]), ObjectId(row[1])) for row in page)
        ids = tuple(pair[0] for pair in pairs)
        if any(left >= right for left, right in zip((after, *ids), ids)):
            raise ValueError("动作读取页不是严格递增的有效身份")
        yield pairs
        after = ids[-1]
        if len(page) < batch_size:
            return


def require_target_facts(check: Mapping, activity: Mapping, action: Mapping) -> None:
    """核对一项仍持有限制的旧活动，不判断调用收场或目标集合完整性。"""
    if (not {"action_id", "device_id", "driver_id", "expected_identity_json", "resource_key"} <= check.keys()
            or not {"id", "action_id", "activity_state", "occupancy_state", "ownership_mode",
                    "dispatch_state", "availability_release_attempt_id", "expected_identity_json",
                    "resource_key"} <= activity.keys()
            or not {"id", "type", "status", "execution_started", "device_id", "driver_id",
                    "execution_spec_json"} <= action.keys()):
        raise AvailabilityTargetStateError("检查或旧活动缺少必要的固定事实")
    try:
        ObjectId(check["action_id"])
        ObjectId(activity["id"])
        ObjectId(activity["action_id"])
        ObjectId(action["id"])
    except (KeyError, TypeError, ValueError) as error:
        raise AvailabilityTargetStateError("检查与旧活动缺少有效动作身份") from error
    if (any(type(action.get(name)) is not int for name in ("type", "status", "execution_started"))
            or any(type(activity.get(name)) is not int for name in
                   ("activity_state", "occupancy_state", "ownership_mode", "dispatch_state"))):
        raise AvailabilityTargetStateError("旧活动状态必须是有效整数")
    if not isinstance(check["device_id"], str) or not check["device_id"]:
        raise AvailabilityTargetStateError("检查缺少有效逻辑设备身份")
    if (activity["action_id"] != action["id"] or activity["availability_release_attempt_id"] is not None):
        raise AvailabilityTargetStateError("旧活动的动作归属或释放引用矛盾")
    action_status = enum_for("actions.status")
    activity_state = enum_for("device_activities.activity_state")
    occupancy = enum_for("device_activities.occupancy_state")
    ownership = enum_for("device_activities.ownership_mode")
    dispatch = enum_for("device_activities.dispatch_state")
    if (not is_hasselblad_photo(action)
            or action.get("status") != int(action_status.FAILED)
            or action.get("execution_started") != 1
            or action.get("id") == check.get("action_id")
            or activity.get("activity_state") != int(activity_state.UNKNOWN)
            or activity.get("occupancy_state") != int(occupancy.HELD)
            or activity.get("ownership_mode") != int(ownership.TASK_SCOPE)
            or activity.get("dispatch_state") not in (
                int(dispatch.MAY_HAVE_DISPATCHED), int(dispatch.SUCCESS_RETURNED))
            or action.get("device_id") != check.get("device_id")
            or action.get("driver_id") != check.get("driver_id")):
        raise AvailabilityTargetBlocked("旧活动不符合可用性检查目标的固定条件")
    try:
        identity = CameraIdentity(**check["expected_identity_json"])
        original = CameraIdentity(**activity["expected_identity_json"])
    except (KeyError, TypeError, ValueError) as error:
        raise AvailabilityTargetStateError("检查或旧活动缺少有效固定机身身份") from error
    if (not json_equal(check["expected_identity_json"], activity["expected_identity_json"])
            or original != identity
            or check.get("resource_key") != identity.resource_key
            or activity.get("resource_key") != identity.resource_key):
        raise AvailabilityTargetIdentityMismatch("检查与旧活动的固定机身身份不一致")


def iter_creation_targets(connection, check: Mapping, *, batch_size: int = 128):
    """从原写事务读取检查的完整旧目标；结果不授予设备执行资格。"""
    from camctl.persistence.transaction import row_facts
    from .capture_facts import load_start_facts

    for page in iter_logical_held_activities(connection, check["device_id"], batch_size=batch_size):
        for action_id, activity_id in page:
            activity = row_facts(connection, "device_activities", activity_id)
            if (activity is None or activity.get("action_id") != action_id
                    or not activity.get("resource_key")):
                raise AvailabilityTargetStateError("逻辑设备旧活动缺少固定身份或归属矛盾")
            if activity["resource_key"] != check["resource_key"]:
                raise AvailabilityTargetIdentityMismatch("逻辑设备旧活动的固定机身身份不同")

    running = int(enum_for("operation_attempts.status").RUNNING)
    statuses = enum_for("operation_runs.status")
    kinds = enum_for("operation_runs.kind")
    purposes = enum_for("operation_runs.query_purpose")
    seen = False
    for page in iter_held_activity_ids(connection, check["resource_key"], batch_size=batch_size):
        for activity_id in page:
            activity = row_facts(connection, "device_activities", activity_id)
            action = None if activity is None else row_facts(connection, "actions", activity["action_id"])
            if activity is None or action is None:
                raise AvailabilityTargetStateError("旧活动缺少实际动作或活动事实")
            with closing(connection.execute(
                "SELECT check_id FROM device_availability_targets WHERE action_id=? AND activity_id=? LIMIT 1",
                (check["action_id"], activity_id),
            )) as cursor:
                existing = cursor.fetchone()
            if existing is not None:
                raise AvailabilityTargetReuse(existing[0])
            require_target_facts(check, activity, action)
            with closing(connection.execute(
                "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
                " WHERE r.activity_id=? AND (t.status=? OR t.result_event_id IS NULL) LIMIT 1",
                (activity_id, running),
            )) as cursor:
                if cursor.fetchone() is not None:
                    raise AvailabilityTargetBlocked("旧活动存在尚未保存结果的实际尝试")
            start = load_start_facts(connection, action)
            if (start.activity is None or start.activity.get("id") != activity_id
                    or start.run is None or type(start.attempts_used) is not int or start.attempts_used < 1
                    or type(start.run.get("status")) is not int):
                raise AvailabilityTargetStateError("旧活动缺少一致的 START 责任")
            if (start.run["status"] in (int(statuses.PENDING), int(statuses.ACTIVE))
                    or start.not_started or not start.preparation_resolved):
                raise AvailabilityTargetBlocked("旧活动的 START 或准备责任尚未形成适用收场")
            with closing(connection.execute(
                "SELECT 1 FROM operation_runs WHERE action_id=? AND kind=? AND query_purpose=?"
                " AND status IN (?,?) LIMIT 1",
                (action["id"], int(kinds.QUERY_ACTIVITY), int(purposes.START_CONFIRMATION),
                 int(statuses.PENDING), int(statuses.ACTIVE)),
            )) as cursor:
                if cursor.fetchone() is not None:
                    raise AvailabilityTargetBlocked("旧活动的 START 核实责任尚未终结")
            with closing(connection.execute(
                "SELECT 1 FROM operation_attempts t, json_each(t.result_json,'$.observations') observation"
                " WHERE t.run_id=? AND t.result_event_id IS NOT NULL"
                " AND json_extract(observation.value,'$.type')='photo_taken' LIMIT 1",
                (start.run["id"],),
            )) as cursor:
                if cursor.fetchone() is not None:
                    raise AvailabilityTargetBlocked("旧活动已有照片结果，必须沿原文件责任处理")
        seen = True
        yield page
    if not seen:
        raise AvailabilityTargetNotRequired("没有仍持有限制的检查目标")
