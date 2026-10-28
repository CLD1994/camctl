"""读取同一设备的拍摄候选，不把候选本身解释成执行资格。"""

from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal

from camctl.contracts.values import ObjectId, UtcMicros
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
        parameters = (request.device_id, request.driver_id, photo, pending)
        if after is not None:
            parameters += after
        with closing(connection.execute(
            "SELECT a.id,a.scheduled_at,a.plan_id,a.input_index,a.max_delay_ms,d.id"
            " FROM actions a LEFT JOIN device_activities d ON d.action_id=a.id"
            " WHERE a.device_id=? AND a.driver_id=? AND a.type=? AND a.status=?"
            " AND a.execution_started=0 AND a.cancel_requested=0"
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
                raise ValueError("拍摄候选排序不严格递增")
            after = order
            lower = Decimal(scheduled) - request.prepare_lead_s * 1_000_000
            if Decimal(request.trusted_wall_now) < lower:
                return
            if request.trusted_wall_now <= scheduled + delay * 1000:
                yield CaptureCandidate(action_id, scheduled, plan_id, index, delay, activity_id)
        if len(page) < batch_size:
            return
