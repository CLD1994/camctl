"""受理服务保留数据库结果中的原异常与控制对象。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import (
    AcceptanceContext, AcceptanceStateError, CommandMode, accept_input,
)
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcome, DbOutcomeKind, db_outcome_for
from camctl.persistence.runtime import DatabaseMetadata

pytestmark = pytest.mark.asyncio


def context():
    return AcceptanceContext(CommandMode.SUBMIT, None, None)


@pytest.mark.parametrize("kind", [DbOutcomeKind.NOT_EXECUTED, DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
async def test_failed_admission_preserves_primary_and_secondary_database_errors(kind):
    primary = OSError(5, "保存输入失败", "/state")
    secondary = RuntimeError("原连接收场失败")
    actual = DbOutcome(kind, error=primary, secondary_errors=(secondary,))
    database = SimpleNamespace(
        metadata=DatabaseMetadata("camctl", "test-instance", 1, "/staging", "/ready", "/processing"),
        write=AsyncMock(return_value=actual),
    )
    with pytest.raises(AcceptanceStateError) as caught:
        await accept_input(ParsedInput("/input", {}), context(), new_operation_key(), database)
    assert caught.value.__cause__.exceptions == (primary, secondary)
    assert db_outcome_for(caught.value) is actual
    assert caught.value.outcome_kind is kind
    assert database.write.await_count == 1


async def test_failed_admission_keeps_original_control_object_and_actual_result():
    control = asyncio.CancelledError("原输入取消")
    secondary = OSError(5, "原回滚失败", "/state")
    actual = DbOutcome(DbOutcomeKind.UNKNOWN, control=control, secondary_errors=(secondary,))
    database = SimpleNamespace(
        metadata=DatabaseMetadata("camctl", "test-instance", 1, "/staging", "/ready", "/processing"),
        write=AsyncMock(return_value=actual),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        await accept_input(ParsedInput("/input", {}), context(), new_operation_key(), database)
    assert caught.value is control
    assert db_outcome_for(control) is actual
    assert any("原回滚失败" in note for note in control.__notes__)
