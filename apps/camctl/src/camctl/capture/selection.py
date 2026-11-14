"""拍摄候选选择的原始业务申请及固定格式。"""

from dataclasses import dataclass
from decimal import Decimal

from camctl.contracts.values import ConsistencyError, UtcMicros
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.operations.capture_selection import CaptureSelectionRequest


@dataclass(frozen=True)
class CaptureSelectionInput(CaptureSelectionRequest):
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


def may_prepare_availability_check(action):
    """仅尚未启动、未取消的待处理哈苏拍摄可触发检查。"""
    from camctl.contracts.enums import enum_for
    from camctl.devices.start_entry import is_hasselblad_photo
    return (is_hasselblad_photo(action)
            and action["status"] == int(enum_for("actions.status").PENDING)
            and action["execution_started"] == 0 and action["cancel_requested"] == 0)


def within_prepare_window(action, lead_s, now):
    """检查时刻必须位于固定拍摄时间及提前量允许的范围内。"""
    return (Decimal(action["scheduled_at"]) - lead_s * 1_000_000
            <= now <= action["scheduled_at"] + action["max_delay_ms"] * 1000)


def earlier_capture_preempts(selected, action, *, for_read):
    """较早的合格拍摄优先；读取检查让位于任一其他拍摄。"""
    return (selected is not None and selected.action_id != action["id"]
            and (for_read or (selected.scheduled_at, selected.plan_id, selected.input_index)
                 < (action["scheduled_at"], action["plan_id"], action["input_index"])))
