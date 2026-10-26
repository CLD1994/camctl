"""锁错误的原始与次级事实进入共同异常图，包括原控制对象。"""

import asyncio
import json
from pathlib import Path

import pytest

from camctl.operations.error_diagnostics import exception_diagnostic
from camctl.session.locks import (
    LockActionResult, LockAcquisitionResult, LockError, LockOperation,
    _LockErrorFacts, _attach_lock_facts,
)


@pytest.mark.parametrize("control", [False, True])
def test_lock_facts_and_secondary_errors_survive_exception_encoding(control):
    primary = asyncio.CancelledError("原控制") if control else OSError(5, "加锁失败")
    secondary = OSError(13, "关闭失败", "/locks/session")
    close = _LockErrorFacts(LockOperation.CLOSE, Path("/locks/session"), secondary,
                           LockAcquisitionResult.UNCONFIRMED,
                           LockActionResult.NOT_ATTEMPTED, LockActionResult.UNCONFIRMED)
    facts = _LockErrorFacts(LockOperation.TRY_LOCK, Path("/locks/session"), primary,
                           LockAcquisitionResult.UNCONFIRMED,
                           LockActionResult.NOT_ATTEMPTED, LockActionResult.UNCONFIRMED,
                           (close,))
    error = primary if control else LockError("锁操作失败", facts=facts)
    if control:
        _attach_lock_facts(error, facts)
    document = json.loads(json.dumps(exception_diagnostic(error)))
    lock = document["diagnostic_facts"]["lock"]
    assert lock["operation"] == "try_lock"
    assert lock["path"] == "/locks/session"
    assert lock["secondary_errors"][0]["original_error"]["filename"] == "/locks/session"
    assert lock["secondary_errors"][0]["original_error"]["errno"] == 13
    if control:
        assert lock["original_error"] == {"$ref": "#"}
    else:
        assert lock["original_error"]["errno"] == 5
