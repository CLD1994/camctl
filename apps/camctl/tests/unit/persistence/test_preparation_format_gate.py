"""准备仓储按原动作类型在提交前核对格式资格。"""

from decimal import Decimal
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
from camctl.persistence import runtime, transaction
from camctl.persistence.repositories import operations, scheduling


def _application(entrypoint, owned):
    key = new_operation_key()
    if entrypoint == "reserve":
        request = scheduling.ReservePreparationRequest(7, 1, 1, Decimal("10"))
        return lambda: scheduling.SchedulingRepository().reserve_preparation(request, key, owned)
    if entrypoint == "failure":
        request = scheduling.FailPreparationRequest(7, 1, "device_failed")
        return lambda: scheduling.SchedulingRepository().fail_preparation(request, key, owned)
    intent = AttemptIntent("control", 7, OperationKind.PREPARE_CAPTURE, AttemptTarget(activity_id=11),
        None, AttemptConfig(1, timeout_s=Decimal("10")), 1, session_key="a" * 32)
    return lambda: operations.OperationRepository().begin_attempt(intent, key, owned)


@pytest.mark.parametrize("entrypoint", ["reserve", "failure", "attempt"])
@pytest.mark.parametrize("action_type", [1, 2])
def test_preparation_write_passes_original_action_type_to_format_gate(mocker, entrypoint, action_type):
    owned = SimpleNamespace(connection=create_autospec(sqlite3.Connection, instance=True),
        metadata=SimpleNamespace(format_version=4))
    mocker.patch.object(transaction, "row_facts", autospec=True, return_value={"id": 7, "type": action_type})
    def required_format(format_version, *, action_type=None):
        raise runtime.DatabaseUnsupportedFormatError("原格式不支持该准备申请")
    gate = create_autospec(required_format, side_effect=required_format)
    mocker.patch.object(runtime, "require_capture_preparation_format", new=gate)
    scheduling_commit = mocker.patch.object(scheduling, "commit_operation", autospec=True)
    operations_commit = mocker.patch.object(operations, "commit_operation", autospec=True)
    apply = _application(entrypoint, owned)
    with pytest.raises(runtime.DatabaseUnsupportedFormatError):
        apply()
    gate.assert_called_once_with(4, action_type=action_type)
    scheduling_commit.assert_not_called()
    operations_commit.assert_not_called()
