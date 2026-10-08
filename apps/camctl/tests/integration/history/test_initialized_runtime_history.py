"""初始化单例在首条自身历史前已经存在，恢复不能把零计数当作未出生。"""

from contextlib import closing

import pytest

from camctl.contracts.enums import load_registry
from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.decoding import decode_event_row
from camctl.history.events import branch_of, event_type_name
from camctl.history.replay import EntityImage, ReplayError, RestoreSeed, restore
from camctl.history.validators import ValidatedEvent
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.session import SessionRepository
from camctl.persistence.runtime import DatabaseInvalidError
from camctl.persistence.transaction import row_facts
from camctl.session.clock import ClockCheck, ClockTrustStatus, register_clock_guard

from ..persistence.test_motor_transactions import NOW, _admit, owned


_INITIAL_ROW = {
    "id": 1,
    "acknowledged_wm": 0,
    "acknowledged_report_id": None,
    "trusted_time_lower_bound": None,
    "trusted_time_event_id": None,
    "cleanup_cursor_file_id": None,
}


def _links(owned):
    entity_type = load_registry()["history_objects"]["runtime_state"]["id"]
    with closing(owned.connection.execute(
            "SELECT event_id, change_count FROM entity_event_links"
            " WHERE entity_type = ? AND entity_id = 1 ORDER BY event_id",
            (entity_type,))) as cursor:
        return cursor.fetchall()


def test_initialized_runtime_exists_at_initial_boundary_without_history_links(owned, tmp_path):
    """初始化行已有完整值；没有目录关联不表示该单例不存在。"""
    history = HistoryRepository(tmp_path / "state.db")
    assert row_facts(owned.connection, "runtime_state", 1) == _INITIAL_ROW
    assert history.current_boundary() == INITIAL_BOUNDARY
    assert _links(owned) == []

    actual = history.restore_entity("runtime_state", 1, INITIAL_BOUNDARY)

    assert actual == {("runtime_state", 1): _INITIAL_ROW}
    assert _links(owned) == []
    assert history.current_boundary() == INITIAL_BOUNDARY
    assert row_facts(owned.connection, "runtime_state", 1) == _INITIAL_ROW


def test_initialized_runtime_exists_at_unrelated_complete_h_without_history_links(owned, tmp_path):
    """受理已经推进全库边界，但尚未改变全局单例时仍可读取它。"""
    _admit(owned)
    history = HistoryRepository(tmp_path / "state.db")
    boundary = history.current_boundary()
    assert boundary != INITIAL_BOUNDARY
    assert _links(owned) == []
    assert row_facts(owned.connection, "runtime_state", 1) == _INITIAL_ROW

    actual = history.restore_entity("runtime_state", 1, boundary)

    assert actual == {("runtime_state", 1): _INITIAL_ROW}
    assert _links(owned) == []
    assert history.current_boundary() == boundary
    assert row_facts(owned.connection, "runtime_state", 1) == _INITIAL_ROW


@pytest.mark.parametrize("target", ["initial", "before_first_runtime_change"])
def test_initialized_runtime_restores_initial_values_before_first_change(owned, tmp_path, target):
    """撤回首次 CLOCK_ACCEPTED 后保留初始单例，不借用较新的可信时间。"""
    _admit(owned)
    history = HistoryRepository(tmp_path / "state.db")
    before_runtime = history.current_boundary()
    boundary = INITIAL_BOUNDARY if target == "initial" else before_runtime
    assert before_runtime != INITIAL_BOUNDARY
    assert _links(owned) == []
    register_clock_guard()
    outcome = SessionRepository().update_lower_bound(
        ClockCheck(ClockTrustStatus.TRUSTED, NOW, None, NOW),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    current_boundary = history.current_boundary()
    assert current_boundary.last_event_id > before_runtime.last_event_id
    assert _links(owned) == [(current_boundary.last_event_id, 1)]
    current_row = row_facts(owned.connection, "runtime_state", 1)
    assert current_row == {
        **_INITIAL_ROW,
        "trusted_time_lower_bound": NOW,
        "trusted_time_event_id": current_boundary.last_event_id,
    }

    actual = history.restore_entity("runtime_state", 1, boundary, event_batch_size=1)

    assert actual == {("runtime_state", 1): _INITIAL_ROW}
    assert row_facts(owned.connection, "runtime_state", 1) == current_row
    assert history.current_boundary() == current_boundary
    assert _links(owned) == [(current_boundary.last_event_id, 1)]


def test_pure_restore_preserves_initialized_runtime_across_first_real_change(owned, tmp_path):
    """真实 CLOCK 正向应用后再逆向回到初始化，保留单例及零计数。"""
    register_clock_guard()
    outcome = SessionRepository().update_lower_bound(
        ClockCheck(ClockTrustStatus.TRUSTED, NOW, None, NOW),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    history = HistoryRepository(tmp_path / "state.db")
    boundary = history.current_boundary()
    with closing(owned.connection.execute(
            "SELECT id, transaction_id, event_type, event_version, occurred_at,"
            " clock_status, change_seq, body_json FROM history_events"
            " WHERE id = ?", (boundary.last_event_id,))) as cursor:
        event = decode_event_row(cursor.fetchone())
    entity_type = load_registry()["history_objects"]["runtime_state"]["id"]
    validated = ValidatedEvent(
        event, event_type_name(event.event_type), branch_of(event.event_type, event.reason)[0],
        ((entity_type, 1),), {("runtime_state", 1): (entity_type, 1)})
    initial_values = {name: value for name, value in _INITIAL_ROW.items() if name != "id"}
    initial = EntityImage(entity_type, 1, True, {("runtime_state", 1): initial_values}, 0, 0)

    current = restore(RestoreSeed(initial, INITIAL_BOUNDARY), (validated,), boundary)

    assert current.rows == {("runtime_state", 1): {
        **initial_values,
        "trusted_time_lower_bound": NOW,
        "trusted_time_event_id": boundary.last_event_id,
    }}
    assert current.exists is True
    assert current.change_count == 1

    restored = restore(RestoreSeed(current, boundary), (validated,), INITIAL_BOUNDARY)

    assert restored == initial
    assert history.current_boundary() == boundary
    assert _links(owned) == [(boundary.last_event_id, 1)]


def test_runtime_with_own_history_keeps_actual_current_values(owned, tmp_path):
    register_clock_guard()
    saved = SessionRepository().update_lower_bound(
        ClockCheck(ClockTrustStatus.TRUSTED, NOW, None, NOW),
        new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    history = HistoryRepository(tmp_path / "state.db")
    boundary = history.current_boundary()

    assert history.restore_entity("runtime_state", 1, boundary) == {
        ("runtime_state", 1): {
            **_INITIAL_ROW,
            "trusted_time_lower_bound": NOW,
            "trusted_time_event_id": boundary.last_event_id,
        },
    }


@pytest.mark.parametrize("fault", ["missing_row", "changed_without_history", "false_boundary"])
def test_initialized_runtime_rejects_unreliable_current_basis(owned, tmp_path, fault):
    _admit(owned)
    history = HistoryRepository(tmp_path / "state.db")
    boundary = history.current_boundary()
    if fault == "missing_row":
        owned.connection.execute("DELETE FROM runtime_state WHERE id=1")
    elif fault == "changed_without_history":
        owned.connection.execute("PRAGMA ignore_check_constraints=ON")
        owned.connection.execute("UPDATE runtime_state SET acknowledged_wm=1 WHERE id=1")
    else:
        boundary = HistoryBoundary(boundary.txn_id + 1, boundary.last_event_id)
    baseline = tuple(owned.connection.iterdump())

    expected = DatabaseInvalidError if fault == "missing_row" else ConsistencyError
    with pytest.raises(expected):
        history.restore_entity("runtime_state", 1, boundary)

    assert tuple(owned.connection.iterdump()) == baseline


@pytest.mark.parametrize("fault", ["missing_initial_column", "wrong_initial_value", "ordinary_object"])
def test_pure_restore_rejects_unreliable_existing_zero_count_seed(fault):
    """存在的零计数种子必须属于初始化单例且表达其完整初始事实。"""
    registry = load_registry()["history_objects"]
    values = {name: value for name, value in _INITIAL_ROW.items() if name != "id"}
    if fault == "missing_initial_column":
        values.pop("acknowledged_wm")
        image = EntityImage(registry["runtime_state"]["id"], 1, True,
                            {("runtime_state", 1): values}, 0, 0)
    elif fault == "wrong_initial_value":
        values["trusted_time_lower_bound"] = NOW
        values["trusted_time_event_id"] = 1
        image = EntityImage(registry["runtime_state"]["id"], 1, True,
                            {("runtime_state", 1): values}, 0, 0)
    else:
        image = EntityImage(registry["plan"]["id"], 1, True,
                            {("plans", 1): {"status": 1}}, 0, 0)

    with pytest.raises(ReplayError):
        restore(RestoreSeed(image, INITIAL_BOUNDARY), (), INITIAL_BOUNDARY)
