"""拍摄能力路由与处理器。

按动作自身定义路由完成声明与处理器：未支持类型明确拒绝，不默
认录像。处理器是调度侧（Q5）登记的动作推进入口。
"""

from __future__ import annotations

from camctl.capture.models import CaptureCompletion
from camctl.scheduling.service import ActionHandler

__all__ = ["capture_handler", "route_completion"]

_HANDLERS: dict[str, ActionHandler] = {}


async def _record_handler(action_id: int, context) -> None:  # pragma: no cover - 阶段 3 实装
    raise NotImplementedError("录像执行随阶段 3 调度录制接入")


async def _photo_handler(action_id: int, context) -> None:  # pragma: no cover - 阶段 4 实装
    raise NotImplementedError("拍照执行随阶段 4 接入")


async def _timelapse_handler(action_id: int, context) -> None:  # pragma: no cover - 阶段 4 实装
    raise NotImplementedError("延时摄影执行随阶段 4 接入")


_HANDLERS.update(
    {
        "camera_record": _record_handler,
        "camera_take_photo": _photo_handler,
        "camera_timelapse": _timelapse_handler,
    }
)


def capture_handler(action_type: str) -> ActionHandler:
    """取得拍摄类型的处理器；未支持类型拒绝（不默认录像）。"""
    return _HANDLERS[action_type]


def route_completion(action_type: str, declared: CaptureCompletion) -> CaptureCompletion:
    """核对完成声明属于该动作类型；返回原声明。"""
    if action_type not in _HANDLERS:
        raise ValueError(f"未支持的拍摄类型: {action_type!r}")
    return declared
