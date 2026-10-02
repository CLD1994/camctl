"""真实保存、历史解码与镜像正逆应用的值连续性组合。"""

from decimal import Decimal

import pytest

from camctl.contracts.history_values import HistoryBoundary, ReadOrder, ReadScope
from camctl.contracts.values import new_operation_key
from camctl.history.replay import EntityImage, ReplayError, apply_forward, apply_reverse
from camctl.history.validators import ValidatedEvent
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.transaction import commit_operation

from ..persistence.test_runtime import _create_valid_database
from ..persistence.test_transactions import PlanCreateCommand, PlanStartCommand, _open, plan_guards  # noqa: F401


@pytest.fixture
def saved_history(tmp_path, plan_guards):
    _create_valid_database(tmp_path / "state.db")
    owned = _open(tmp_path)
    try:
        for command in (PlanCreateCommand((1,)), PlanStartCommand(1, before_status=1)):
            receipt = commit_operation(command, new_operation_key(), owned)
            assert receipt.kind == "completed", receipt.error
        page = HistoryRepository(tmp_path / "state.db").read_events(
            ReadScope(ReadOrder.ASCENDING, None, 1, 2, 8, lambda value: value), HistoryBoundary(2, 2),
        )
        events = tuple(ValidatedEvent(
            envelope, "PLAN_ACCEPTED" if envelope.event_type == 1 else "PLAN_STATUS_CHANGED",
            "CREATE" if envelope.event_type == 1 else "START", ((4, 1),),
            {("plans", 1): (4, 1)},
        ) for envelope in page.items)
        yield owned.connection, events
    finally:
        owned.connection.close()


def _image(values, count):
    return EntityImage(4, 1, True, {("plans", 1): values}, count, count)


@pytest.mark.parametrize("direction", ["forward", "reverse"])
@pytest.mark.parametrize("invalid", ["missing", "null"])
def test_stored_update_rejects_missing_or_null_status(saved_history, direction, invalid):
    connection, events = saved_history
    values = {"status": None} if invalid == "null" else {}
    before = tuple(connection.iterdump())
    apply = apply_forward if direction == "forward" else apply_reverse
    with pytest.raises(ReplayError):
        apply(_image(values, 1 if direction == "forward" else 2), events[1])
    assert tuple(connection.iterdump()) == before


def test_stored_forward_update_rejects_boolean_status(saved_history):
    connection, events = saved_history
    before = tuple(connection.iterdump())
    with pytest.raises(ReplayError):
        apply_forward(_image({"status": True}, 1), events[1])
    assert tuple(connection.iterdump()) == before


def test_stored_reverse_creation_rejects_corrupted_business_row(saved_history):
    connection, events = saved_history
    values = dict(events[0].envelope.rows[0].after.values)
    values["status"] = True
    before = tuple(connection.iterdump())
    with pytest.raises(ReplayError):
        apply_reverse(_image(values, 1), events[0])
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("direction,original,expected", [
    ("forward", Decimal("1.0"), 2), ("reverse", Decimal("2.0"), 1),
])
def test_stored_update_accepts_equal_json_integer_values(saved_history, direction, original, expected):
    _, events = saved_history
    apply = apply_forward if direction == "forward" else apply_reverse
    result = apply(_image({"status": original}, 1 if direction == "forward" else 2), events[1])
    assert result.rows[("plans", 1)] == {"status": expected}


def test_stored_reverse_creation_removes_matching_complete_row(saved_history):
    _, events = saved_history
    values = dict(events[0].envelope.rows[0].after.values)
    values["status"] = Decimal("1.0")
    result = apply_reverse(_image(values, 1), events[0])
    assert result.rows == {}
    assert result.exists is False
    assert result.change_count == 0
