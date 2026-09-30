"""Q5 按需动作执行器的单元测试。

只为已具备推进步骤的责任创建协程：阻塞动作没有执行器；错误类
型不默认路由录像；结束后释放。
"""

from __future__ import annotations

import pytest

from camctl.scheduling.service import (
    ActionDescriptor,
    HandlerRegistry,
    ReadyCheck,
    collect_ready,
)


def _descriptor(action_id: int, *, action_type: str = "camera_take_photo", ready: bool,
                blocked_reason: str | None = None) -> ActionDescriptor:
    return ActionDescriptor(
        action_id=action_id,
        action_type=action_type,
        ready=ready,
        blocked_reason=blocked_reason,
    )


class TestCollectReady:
    def test_blocked_actions_have_no_executor(self) -> None:
        descriptors = [
            _descriptor(1, ready=False, blocked_reason="future_scheduled_at"),
            _descriptor(2, ready=False, blocked_reason="source_not_finished"),
            _descriptor(3, ready=False, blocked_reason="device_occupied"),
            _descriptor(4, ready=True),
        ]
        ready = collect_ready(descriptors)
        assert ready == [4]

    def test_no_descriptors_means_no_executors(self) -> None:
        assert collect_ready([]) == []

    def test_executor_created_only_for_own_responsibility(self) -> None:
        # 准备按需创建：描述符数量即潜在责任，不预建整个动作协程。
        descriptors = [_descriptor(index, ready=(index % 2 == 0)) for index in range(1, 7)]
        ready = collect_ready(descriptors)
        assert ready == [2, 4, 6]


class TestHandlerRegistry:
    def test_handler_registered_per_action_type(self) -> None:
        registry = HandlerRegistry()
        handler = object()
        registry.register_handler("camera_take_photo", handler)
        assert registry.handler_for("camera_take_photo") is handler

    def test_unknown_type_returns_none_not_camera(self) -> None:
        registry = HandlerRegistry()
        registry.register_handler("camera_take_photo", object())
        # 错误类型不默认路由录像。
        assert registry.handler_for("obtain_action_outputs") is None

    def test_overwrite_registration(self) -> None:
        registry = HandlerRegistry()
        first, second = object(), object()
        registry.register_handler("report_status", first)
        registry.register_handler("report_status", second)
        assert registry.handler_for("report_status") is second
