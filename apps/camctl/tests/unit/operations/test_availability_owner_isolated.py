"""原检查结果的会话拥有者与协作者隔离。"""

import asyncio
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from camctl import resources


@pytest.fixture
def owner_type(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("不能读取包资源"))
    diagnostics = ModuleType("camctl.logging_runtime.diagnostics")
    diagnostics.DiagnosticState = type("DiagnosticState", (), {})
    calls = ModuleType("camctl.operations.owned_calls")
    calls.owned_tool_call = lambda function: function
    business = ModuleType("camctl.persistence.business")

    class SharedSave:
        def __init__(self):
            self.task = None

        async def run(self, callback):
            if self.task is None:
                self.task = asyncio.create_task(callback())
            return await asyncio.shield(self.task)

        def retry_failed(self):
            if self.task is not None and self.task.done() and self.task.exception():
                self.task = None

    business.SharedSave = SharedSave
    for module in (diagnostics, calls, business):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    source = find_spec("camctl.operations.availability_owner")
    spec = spec_from_file_location("camctl.operations._isolated_availability_owner", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module.AvailabilityAttemptOwner


@pytest.mark.asyncio
async def test_concurrent_observers_share_one_call_and_save(owner_type):
    actual = object()
    invoke, save = AsyncMock(return_value=actual), AsyncMock()
    owner = owner_type("ticket", invoke, save, lambda error: None)

    await asyncio.gather(owner.advance(), owner.advance())

    invoke.assert_awaited_once()
    save.assert_awaited_once_with(actual, owner.diagnostic)
    assert owner.saved


@pytest.mark.asyncio
async def test_cancel_delivers_result_before_propagating(owner_type):
    actual = object()
    started = asyncio.Event()

    async def invoke():
        started.set()
        await asyncio.Event().wait()

    save = AsyncMock()
    owner = owner_type("ticket", invoke, save, lambda error: actual)
    observer = asyncio.create_task(owner.advance())
    await started.wait()
    owner.stop()
    with pytest.raises(asyncio.CancelledError):
        await observer

    save.assert_awaited_once_with(actual, owner.diagnostic)
    assert owner.saved


@pytest.mark.asyncio
async def test_failed_save_retries_original_result(owner_type):
    actual = object()
    invoke = AsyncMock(return_value=actual)
    save = AsyncMock(side_effect=[RuntimeError("receipt unknown"), None])
    owner = owner_type("ticket", invoke, save, lambda error: None)

    with pytest.raises(RuntimeError):
        await owner.advance()
    with pytest.raises(RuntimeError):
        await owner.settle()

    invoke.assert_awaited_once()
    assert save.await_count == 2
    assert all(call.args[0] is actual for call in save.await_args_list)
    assert owner.saved
