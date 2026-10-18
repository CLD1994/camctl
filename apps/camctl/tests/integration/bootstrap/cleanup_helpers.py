"""直接推进清理轮次的测试边界；调度与实际结果交付分别等待。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from camctl.session.service import StateDbFailure


async def finish_cleanup_round(flow, context):
    await flow(context)
    await flow.settle()


@contextmanager
def cleanup_failure(expected, *, match=None):
    """完整组合交付原错误及尚未交接责任，旧断言继续检查原错误。"""
    result = SimpleNamespace(value=None, combined=None)
    with pytest.raises((expected, ExceptionGroup)) as caught:
        yield result
    failure = caught.value
    result.combined = failure
    if isinstance(failure, ExceptionGroup):
        assert len(failure.exceptions) == 2
        failure, pending = failure.exceptions
        assert isinstance(pending, StateDbFailure)
        assert str(pending) == '原清理保存责任尚未可靠交接'
    assert isinstance(failure, expected)
    if match is not None:
        import re
        assert re.search(match, str(failure))
    result.value = failure
