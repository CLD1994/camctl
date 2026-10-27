"""取消收场等待已持久化但尚未交付的设备检查尝试。"""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [1, 4])
async def test_cancel_waits_for_old_unsettled_check(kind):
    path = Path(__file__).parents[3] / "src/camctl/cancellation/settlement.py"
    source = ast.parse(path.read_text())
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "TargetSettlement")
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "settle")
    program = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                               method], type_ignores=[])
    namespace = {"_fetchone": lambda *args: (kind, 6), "_availability_attempt_pending": lambda *args: True,
        "_CAPTURE_TYPES": frozenset({1, 2, 3}), "_OBTAIN_TYPE": 4,
        "SettlementOutcome": lambda **kwargs: SimpleNamespace(**kwargs)}
    exec(compile(ast.fix_missing_locations(program), str(path), "exec"), namespace)

    async def read(callback, **kwargs):
        return callback(SimpleNamespace(connection=object()))

    capture = AsyncMock()
    target = SimpleNamespace(_database=SimpleNamespace(read=read), _availability_owner=None,
                             _settle_capture=capture)
    result = await namespace["settle"](target, 7)
    assert result.complete is False
    capture.assert_not_awaited()
