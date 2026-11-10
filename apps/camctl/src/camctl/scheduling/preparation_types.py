"""准备阶段绑定失败的原始业务申请。"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from camctl.contracts.values import ObjectId, UtcMicros
from camctl.devices.drivers.hasselblad.identity import CameraIdentity


@dataclass(frozen=True)
class FailBindingPreparationRequest:
    action_id: int
    trusted_wall_now: int
    occurred_at: int
    details: Mapping[str, Any]
    prepare_lead_s: Decimal = Decimal(0)
    current_identity: CameraIdentity | None = None

    def __post_init__(self):
        from camctl.contracts.workflow_errors import validate_error_details
        ObjectId(self.action_id)
        UtcMicros(self.trusted_wall_now)
        UtcMicros(self.occurred_at)
        validate_error_details("device_binding_unavailable", self.details)
        if self.current_identity is not None and not isinstance(self.current_identity, CameraIdentity):
            raise TypeError("当前机身身份必须是 CameraIdentity")
        if (isinstance(self.prepare_lead_s, bool)
                or not isinstance(self.prepare_lead_s, (int, Decimal))
                or not Decimal(self.prepare_lead_s).is_finite() or self.prepare_lead_s < 0):
            raise ValueError("准备提前量必须是有限非负秒数")
