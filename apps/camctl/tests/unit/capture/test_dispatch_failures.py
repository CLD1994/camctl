"""拍摄分发在执行前提失效时停止同一批动作。"""

import sqlite3
import asyncio
from unittest.mock import create_autospec

import pytest

from camctl.capture import dispatch
from camctl.capture.handlers import CaptureRuntime
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceConfigurationError
from camctl.scheduling.service import ActionDescriptor


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    sqlite3.OperationalError("状态查询不可用"),
    ConsistencyError("原责任事实不一致"),
    DeviceConfigurationError("本次设备声明不可可靠解释"),
    asyncio.CancelledError(),
])
async def test_dispatch_prerequisite_failure_stops_remaining_actions(monkeypatch, failure):
    runtime = create_autospec(CaptureRuntime, instance=True, spec_set=True)
    calls = []

    async def handler(action_id, context):
        calls.append(action_id)
        if action_id == 1:
            raise failure

    monkeypatch.setattr(dispatch, "capture_handler", lambda action_type: handler)

    outcomes = await dispatch.dispatch_ready(runtime, [
        ActionDescriptor(1, "camera_take_photo", True),
        ActionDescriptor(2, "camera_take_photo", True),
    ])

    assert calls == [1]
    assert outcomes == [(1, failure)]


@pytest.mark.asyncio
async def test_dispatch_ordinary_action_failure_preserves_remaining_actions(monkeypatch):
    runtime = create_autospec(CaptureRuntime, instance=True, spec_set=True)
    failure = ValueError("当前动作输入不适用")
    calls = []

    async def handler(action_id, context):
        calls.append(action_id)
        if action_id == 1:
            raise failure

    monkeypatch.setattr(dispatch, "capture_handler", lambda action_type: handler)

    outcomes = await dispatch.dispatch_ready(runtime, [
        ActionDescriptor(1, "camera_take_photo", True),
        ActionDescriptor(2, "camera_take_photo", True),
    ])

    assert calls == [1, 2]
    assert outcomes[0] == (1, failure)
    assert outcomes[1][0] == 2
    assert outcomes[1][1].phase == "dispatched"
