"""精确配置读取复用创建事实边界，数值沿用 AttemptConfig 校验。"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptTicket
from camctl.outputs import cleanup_results


TICKET = AttemptTicket(2, "delete", "7", "delete/7", 9)


def _read(monkeypatch, values, database_id="database"):
    reader = create_autospec(cleanup_results._read_cleanup_attempt_creation,
                            return_value=({}, values))
    monkeypatch.setattr(cleanup_results, "_read_cleanup_attempt_creation", reader)
    owned = SimpleNamespace(metadata=SimpleNamespace(instance_id="database"), connection=object())
    return cleanup_results.cleanup_attempt_config(owned, TICKET, database_id=database_id, item_id=7)


@pytest.mark.parametrize("timeout,interval", [(None, None), (Decimal("7.125"), Decimal("0")),
    (Decimal("7.125000000000000000000000001"), Decimal("0.625000000000000000000000001"))])
def test_exact_original_config_preserves_nullable_and_decimal_values(monkeypatch, timeout, interval):
    result = _read(monkeypatch, {"max_attempts_used": 3, "timeout_s_json": timeout, "retry_interval_s_json": interval})
    assert (result.max_attempts, result.timeout_s, result.retry_interval_s) == (3, timeout, interval)


@pytest.mark.parametrize("field,value", [("max_attempts_used", True), ("max_attempts_used", 0),
    ("timeout_s_json", True), ("timeout_s_json", Decimal("NaN")), ("timeout_s_json", Decimal("0")),
    ("retry_interval_s_json", False), ("retry_interval_s_json", Decimal("-0.001")),
    ("retry_interval_s_json", Decimal("Infinity"))])
def test_invalid_original_numeric_config_is_diagnosed(monkeypatch, field, value):
    values = {"max_attempts_used": 3, "timeout_s_json": Decimal("7.125"), "retry_interval_s_json": Decimal("0")}
    values[field] = value
    with pytest.raises(ConsistencyError, match="item=7 run=9 attempt=2"):
        _read(monkeypatch, values)


@pytest.mark.parametrize("missing", ["max_attempts_used", "timeout_s_json", "retry_interval_s_json"])
def test_missing_original_config_is_not_nullable_default(monkeypatch, missing):
    values = {"max_attempts_used": 3, "timeout_s_json": None, "retry_interval_s_json": None}
    values.pop(missing)
    with pytest.raises(ConsistencyError):
        _read(monkeypatch, values)


def test_original_config_refuses_another_database_before_creation_read(monkeypatch):
    with pytest.raises(ConsistencyError, match="数据库"):
        _read(monkeypatch, {}, database_id="different")


@pytest.mark.parametrize("mismatch", [None, "database", "item", "ticket", "target", "operation", "consumer"])
@pytest.mark.asyncio
async def test_control_request_uses_matching_frozen_config_and_diagnoses_changed_intent(monkeypatch, mismatch):
    from dataclasses import replace
    from camctl.contracts.values import new_operation_key
    from camctl.devices.bindings import DeviceBinding
    from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
    from camctl.outputs import cleanup_flow
    request = AttemptIntent(operation="delete", action_id=11, kind=OperationKind.DELETE_FILE,
        target=AttemptTarget(cleanup_item_id=7), query_purpose=None,
        config=AttemptConfig(3, Decimal("7.125"), Decimal("0")), occurred_at=1)
    intent = cleanup_flow.PendingCleanupIntent("database", 7, request, new_operation_key(), None, TICKET)
    if mismatch == "database":
        intent.database_id = "different"
    elif mismatch == "item":
        intent.item_id = 8
    elif mismatch == "ticket":
        intent.ticket = replace(TICKET, attempt_id=3)
    elif mismatch == "target":
        intent.request = replace(request, target=AttemptTarget(cleanup_item_id=8))
    elif mismatch == "operation":
        intent.request = replace(request, operation="query", kind=OperationKind.CHECK_FILE_EXISTS)
    elif mismatch == "consumer":
        intent.consumer = cleanup_flow.CleanupQueryConsumer.AFTER_DELETE
    from camctl.persistence.business import BusinessDatabase
    database = create_autospec(BusinessDatabase, instance=True)
    database.metadata = SimpleNamespace(instance_id="database")
    database.read.side_effect = lambda callback, **kw: callback(SimpleNamespace(connection=object()))
    runtime = SimpleNamespace(database=database,
        pending_intents={7: intent}, binding_of=AsyncMock(return_value=DeviceBinding("camera", "driver")),
        delete_config=AttemptConfig(9, Decimal("99"), Decimal("12")))
    monkeypatch.setattr(cleanup_flow, "_target_params", lambda connection, item: {})
    if mismatch is not None:
        with pytest.raises(ConsistencyError):
            await cleanup_flow._control_request(runtime, 7, "delete", TICKET)
    else:
        assert (await cleanup_flow._control_request(runtime, 7, "delete", TICKET)).timeout_s == Decimal("7.125")
