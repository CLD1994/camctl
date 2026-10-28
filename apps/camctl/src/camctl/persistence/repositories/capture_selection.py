"""读取同一设备的拍摄候选，不把候选本身解释成执行资格。"""

from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal

from camctl.contracts.values import ConsistencyError, ObjectId, UtcMicros
from camctl.devices.drivers.hasselblad.identity import CameraIdentity


@dataclass(frozen=True)
class CaptureSelectionInput:
    """本次会话的设备绑定、机身、实际提前量及可信时刻。"""

    device_id: str
    driver_id: str
    identity: CameraIdentity
    prepare_lead_s: Decimal
    trusted_wall_now: int

    def __post_init__(self):
        if not isinstance(self.device_id, str) or not self.device_id:
            raise ValueError("拍摄选择缺少设备身份")
        if not isinstance(self.driver_id, str) or not self.driver_id:
            raise ValueError("拍摄选择缺少驱动身份")
        if not isinstance(self.identity, CameraIdentity):
            raise ValueError("拍摄选择缺少实际机身身份")
        if (not isinstance(self.prepare_lead_s, Decimal)
                or not self.prepare_lead_s.is_finite() or self.prepare_lead_s < 0):
            raise ValueError("拍摄选择提前量必须是有限非负秒数")
        UtcMicros(self.trusted_wall_now)

    def document(self):
        return {"device_id": self.device_id, "driver_id": self.driver_id,
                "identity": {"serial": self.identity.serial, "product_id": self.identity.product_id},
                "prepare_lead_s": self.prepare_lead_s, "trusted_wall_now": self.trusted_wall_now}

    @classmethod
    def from_document(cls, value):
        if (not isinstance(value, dict) or set(value) != {"device_id", "driver_id", "identity",
                                                           "prepare_lead_s", "trusted_wall_now"}):
            raise ConsistencyError("拍摄选择缺少完整原申请")
        try:
            lead = value["prepare_lead_s"]
            if isinstance(lead, bool) or not isinstance(lead, (int, Decimal)):
                raise ValueError("提前量不是精确数字")
            return cls(value["device_id"], value["driver_id"], CameraIdentity(**value["identity"]),
                       Decimal(lead), value["trusted_wall_now"])
        except (KeyError, TypeError, ValueError) as error:
            raise ConsistencyError("拍摄选择的原申请无效") from error


@dataclass(frozen=True)
class CaptureCandidate:
    action_id: int
    scheduled_at: int
    plan_id: int
    input_index: int
    max_delay_ms: int
    activity_id: int | None


def pending_capture_candidates(connection, request, *, batch_size=128):
    """按原排序分页读取时间有效的候选，资源和调用资格另行核对。"""
    from camctl.contracts.enums import enum_for
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID

    if not isinstance(request, CaptureSelectionInput):
        raise TypeError("拍摄选择必须使用完整输入")
    if request.driver_id != DRIVER_ID:
        raise ValueError("该选择入口只适用于 Hasselblad")
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise ValueError("拍摄候选批量必须为 1 至 128")
    photo = int(enum_for("actions.type").CAMERA_TAKE_PHOTO)
    pending = int(enum_for("actions.status").PENDING)
    start = int(enum_for("operation_runs.kind").START)
    after = None
    while True:
        continuation = "" if after is None else " AND (a.scheduled_at,a.plan_id,a.input_index,a.id)>(?,?,?,?)"
        parameters = (request.device_id, request.driver_id, photo)
        if after is not None:
            parameters += after
        with closing(connection.execute(
            "SELECT a.id,a.scheduled_at,a.plan_id,a.input_index,a.max_delay_ms,d.id"
            " FROM actions a LEFT JOIN device_activities d ON d.action_id=a.id"
            " WHERE a.device_id=? AND a.driver_id=? AND a.type=?"
            f" AND a.status={pending} AND a.execution_started=0 AND a.cancel_requested=0"
            + continuation +
            " AND NOT EXISTS (SELECT 1 FROM operation_runs r WHERE r.action_id=a.id AND r.kind=?)"
            " ORDER BY a.scheduled_at,a.plan_id,a.input_index,a.id LIMIT ?",
            (*parameters, start, batch_size),
        )) as cursor:
            page = tuple(cursor.fetchmany(batch_size))
        if not page:
            return
        for action_id, scheduled, plan_id, index, delay, activity_id in page:
            ObjectId(action_id)
            ObjectId(plan_id)
            if activity_id is not None:
                ObjectId(activity_id)
            order = (scheduled, plan_id, index, action_id)
            if after is not None and order <= after:
                raise ConsistencyError("拍摄候选排序不严格递增")
            after = order
            lower = Decimal(scheduled) - request.prepare_lead_s * 1_000_000
            if Decimal(request.trusted_wall_now) < lower:
                return
            if request.trusted_wall_now <= scheduled + delay * 1000:
                yield CaptureCandidate(action_id, scheduled, plan_id, index, delay, activity_id)
        if len(page) < batch_size:
            return


def selected_pending_capture(connection, request, boundary, *, exclude_attempt_id=None):
    """从原连接逐项核对无活动拍摄的检查或普通准备资格。"""
    from camctl.persistence.transaction import row_facts
    from .availability_create import CreateAvailabilityCheck, _qualify
    from .availability_target import (AvailabilityTargetBlocked, AvailabilityTargetIdentityMismatch,
                                      AvailabilityTargetNotRequired, AvailabilityTargetReuse)
    from camctl.contracts.enums import enum_for

    for candidate in pending_capture_candidates(connection, request):
        if candidate.activity_id is not None:
            continue
        check_request = CreateAvailabilityCheck(candidate.action_id, request.identity,
            request.trusted_wall_now, request.prepare_lead_s, request.trusted_wall_now)
        try:
            _qualify(connection, check_request, boundary, exclude_attempt_id=exclude_attempt_id,
                     check_candidate_order=False)
        except AvailabilityTargetNotRequired:
            return candidate
        except AvailabilityTargetReuse as reuse:
            check = row_facts(connection, "device_availability_checks", reuse.check_id)
            if check is None or check.get("action_id") != candidate.action_id:
                raise ConsistencyError("拍摄候选的原检查归属不一致")
            with closing(connection.execute(
                "SELECT status FROM operation_runs WHERE availability_check_id=? AND kind=?",
                (reuse.check_id, int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY)),
            )) as cursor:
                run = cursor.fetchone()
            statuses = enum_for("operation_runs.status")
            if run is None:
                raise ConsistencyError("拍摄候选缺少原检查流程")
            if run[0] not in (int(statuses.PENDING), int(statuses.ACTIVE)):
                continue
            try:
                _qualify(connection, check_request, check["target_boundary_event_id"],
                         existing_check=check, exclude_attempt_id=exclude_attempt_id,
                         check_candidate_order=False)
            except AvailabilityTargetNotRequired:
                return candidate
            except (AvailabilityTargetBlocked, AvailabilityTargetIdentityMismatch):
                continue
            return candidate
        except (AvailabilityTargetBlocked, AvailabilityTargetIdentityMismatch):
            continue
        return candidate
    return None
