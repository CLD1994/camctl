"""输出保存拒绝原因的共同表达。"""

_STAGES = {
    "cancel_requested": "owner_canceled",
    "owner_not_running": "owner_not_running",
    "read_result_unconfirmed": "read_result_unconfirmed",
}


def rejection_stage(reason: str | None) -> str:
    """保留已知原因；未知原因单独交付，不猜测动作状态。"""
    return _STAGES.get(reason or "", "owner_reason_unknown")
