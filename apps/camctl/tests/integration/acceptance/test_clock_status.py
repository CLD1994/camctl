"""受理事务的历史事件保留真实读数并标记为尚未检查。"""

from __future__ import annotations

import pytest

from camctl.acceptance.input import InputDiagnostic, InputStage, ParsedInput
from camctl.acceptance.service import (
    AckDisposition,
    CommandMode,
    PlanDisposition,
    ProcessInput,
    accept_input,
)
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.runtime import OwnedConnection

from .test_acceptance import (
    Catalog,
    _NOW,
    _accept,
    _owned,
    _plan_body,
    _seed_report,
    environment,
)
from ..reporting.test_ack_sync import sync_environment

register_acceptance_guards()

pytestmark = pytest.mark.asyncio

_EXPECTED_UNCHECKED = 1
_ACCEPTED_AT = _NOW + 123_456


def _all_rows(connection, table: str):
    return connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def _trusted_bound(connection):
    return connection.execute(
        "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1"
    ).fetchone()[0]


def _latest_transaction(connection):
    return connection.execute(
        "SELECT id, first_event_id, last_event_id FROM history_transactions"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _assert_latest_transaction(
    connection, expected_types: list[int], occurred_at: int
) -> list[tuple]:
    transaction_id, first_event_id, last_event_id = _latest_transaction(connection)
    events = connection.execute(
        "SELECT id, transaction_id, event_type, occurred_at, clock_status"
        " FROM history_events WHERE transaction_id = ? ORDER BY id",
        (transaction_id,),
    ).fetchall()
    assert [row[0] for row in events] == list(
        range(first_event_id, last_event_id + 1)
    )
    assert [row[1] for row in events] == [transaction_id] * len(expected_types)
    assert [row[2] for row in events] == expected_types
    assert [row[3] for row in events] == [occurred_at] * len(expected_types)
    assert [row[4] for row in events] == [_EXPECTED_UNCHECKED] * len(expected_types)
    return events


def _process_at(owned, body: dict, occurred_at: int, key=None):
    return AcceptanceRepository().process_input(
        ProcessInput(
            source=ParsedInput("input.json", body),
            catalog=Catalog(),
            mode=CommandMode.RUN,
            occurred_at=occurred_at,
        ),
        key or new_operation_key(),
        owned,
    )


async def test_first_acceptance_events_are_unchecked(environment, tmp_path) -> None:
    connection, _ = environment
    bound_before = _trusted_bound(connection)
    body = _plan_body(
        actions=[
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 1000},
            },
            {
                "name": "bad-shots",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"type": "single_shot", "shots": 99},
                "policy": {"max_delay_ms": 1000},
            },
            {
                "name": "fetch-shoot",
                "type": "obtain_action_outputs",
                "scheduled_at": "2026-01-15 10:00:00",
                "params": {
                    "source": {"action_name": "shoot"},
                    "purpose": "manual",
                },
            },
        ]
    )
    result = await _accept(environment, tmp_path, body)

    assert result.plan_disposition is PlanDisposition.REGISTERED
    assert connection.execute(
        "SELECT name, status FROM actions ORDER BY id"
    ).fetchall() == [("shoot", 1), ("bad-shots", 4), ("fetch-shoot", 1)]
    _assert_latest_transaction(connection, [2, 2, 2, 3, 1], _NOW)
    assert _trusted_bound(connection) == bound_before


async def test_reused_request_preserves_old_clock_status(environment, tmp_path) -> None:
    connection, _ = environment
    owned = OwnedConnection(connection=connection, metadata=None)
    key = new_operation_key()
    command = ProcessInput(
        source=ParsedInput("input.json", _plan_body()),
        catalog=Catalog(),
        mode=CommandMode.RUN,
        occurred_at=_ACCEPTED_AT,
    )
    first = AcceptanceRepository().process_input(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED
    assert first.value.plan_disposition is PlanDisposition.REGISTERED
    _assert_latest_transaction(connection, [2, 1], _ACCEPTED_AT)

    # 原键核实不会为同一输入生成第二个事务。
    transaction_snapshot = _all_rows(connection, "history_transactions")
    event_snapshot = _all_rows(connection, "history_events")
    retry = AcceptanceRepository().process_input(command, key, owned)
    assert retry.kind is DbOutcomeKind.COMPLETED
    assert retry.value.plan_disposition is PlanDisposition.REUSED
    assert _all_rows(connection, "history_transactions") == transaction_snapshot
    assert _all_rows(connection, "history_events") == event_snapshot

    # 模拟修复发布前已经存在的事件；幂等请求复用不得改写旧历史。
    connection.execute(
        "UPDATE history_events SET clock_status = 2 WHERE transaction_id = ?",
        (transaction_snapshot[-1][0],),
    )
    old_history = _all_rows(connection, "history_events")
    old_transactions = _all_rows(connection, "history_transactions")
    legacy_retry = AcceptanceRepository().process_input(command, key, owned)
    assert legacy_retry.kind is DbOutcomeKind.COMPLETED
    assert legacy_retry.value.plan_disposition is PlanDisposition.REUSED
    assert _all_rows(connection, "history_events") == old_history
    assert _all_rows(connection, "history_transactions") == old_transactions

    result = await _accept(environment, tmp_path, {"request_id": "42"})
    assert result.plan_disposition is PlanDisposition.REUSED
    assert _all_rows(connection, "history_events") == old_history
    assert _all_rows(connection, "history_transactions") == old_transactions


async def test_reused_request_with_advancing_ack_is_unchecked(
    environment, tmp_path
) -> None:
    connection, _ = environment
    await _accept(environment, tmp_path, _plan_body())
    await _seed_report(environment, tmp_path, report_id=1)
    report_wm = connection.execute(
        "SELECT to_wm FROM reports WHERE id = 1"
    ).fetchone()[0]

    result = await _accept(
        environment,
        tmp_path,
        _plan_body(request_id="42", ack="1"),
    )

    assert result.plan_disposition is PlanDisposition.REUSED
    assert result.ack_disposition is AckDisposition.ABSORBED
    assert connection.execute(
        "SELECT COUNT(*) FROM plans WHERE request_id = 42"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT acknowledged_wm, acknowledged_report_id FROM runtime_state"
    ).fetchone() == (report_wm, 1)
    _assert_latest_transaction(connection, [30], _NOW)


async def test_rejection_diagnostic_is_unchecked(environment, tmp_path) -> None:
    connection, _ = environment
    bound_before = _trusted_bound(connection)

    result = await _accept(environment, tmp_path, _plan_body(actions=[]))

    assert result.plan_disposition is PlanDisposition.REJECTED
    assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert "plan_body_rejected" in connection.execute(
        "SELECT errors_json FROM plan_file_diagnostics"
    ).fetchone()[0]
    _assert_latest_transaction(connection, [27], _NOW)
    assert _trusted_bound(connection) == bound_before


@pytest.mark.parametrize(
    ("stage", "expected_code"),
    [
        (InputStage.READ, "plan_file_read_failed"),
        (InputStage.PARSE, "invalid_json"),
    ],
)
async def test_read_and_parse_diagnostics_are_unchecked(
    environment, stage: InputStage, expected_code: str
) -> None:
    connection, context = environment
    source = InputDiagnostic(
        path="broken.json",
        stage=stage,
        detail="controlled input failure",
    )

    result = await accept_input(
        source, context, new_operation_key(), _owned(environment)
    )

    assert result.plan_disposition is PlanDisposition.REJECTED
    assert expected_code in connection.execute(
        "SELECT errors_json FROM plan_file_diagnostics"
    ).fetchone()[0]
    _assert_latest_transaction(connection, [27], _NOW)


async def test_invalid_ack_diagnostic_is_unchecked(environment, tmp_path) -> None:
    connection, _ = environment
    bound_before = _trusted_bound(connection)

    result = await _accept(
        environment, tmp_path, _plan_body(request_id="43", ack="404")
    )

    assert result.plan_disposition is PlanDisposition.REGISTERED
    assert result.ack_disposition is AckDisposition.INVALID
    assert connection.execute(
        "SELECT acknowledged_wm, acknowledged_report_id FROM runtime_state"
    ).fetchone() == (0, None)
    assert "invalid_ack" in connection.execute(
        "SELECT errors_json FROM plan_file_diagnostics"
    ).fetchone()[0]
    _assert_latest_transaction(connection, [2, 1, 27], _NOW)
    assert _trusted_bound(connection) == bound_before


async def test_ack_and_sync_events_are_unchecked(sync_environment) -> None:
    owned, report = sync_environment
    connection = owned.connection
    bound_before = _trusted_bound(connection)
    result = _process_at(
        owned,
        _plan_body(request_id="100", ack=str(report.report_id)),
        _ACCEPTED_AT,
    )

    assert result.kind is DbOutcomeKind.COMPLETED
    assert result.value.plan_disposition is PlanDisposition.REGISTERED
    assert result.value.ack_disposition is AckDisposition.ABSORBED
    assert result.value.ack_watermark == report.to_wm
    assert connection.execute(
        "SELECT acknowledged_wm, acknowledged_report_id FROM runtime_state"
    ).fetchone() == (report.to_wm, report.report_id)
    _assert_latest_transaction(connection, [2, 1, 30, 29], _ACCEPTED_AT)
    assert connection.execute(
        "SELECT status, ack_report_id FROM state_syncs WHERE id = 1"
    ).fetchone() == (2, report.report_id)
    assert _trusted_bound(connection) == bound_before


async def test_equal_watermark_ack_ends_sync_with_unchecked_time(
    sync_environment,
) -> None:
    owned, report = sync_environment
    connection = owned.connection
    bound_before = _trusted_bound(connection)
    connection.execute(
        "UPDATE runtime_state SET acknowledged_wm = ?, acknowledged_report_id = ?",
        (report.to_wm, report.report_id),
    )

    result = _process_at(
        owned,
        {"request_id": "42", "last_report_id": str(report.report_id)},
        _ACCEPTED_AT,
    )

    assert result.kind is DbOutcomeKind.COMPLETED
    assert result.value.plan_disposition is PlanDisposition.REUSED
    assert result.value.ack_disposition is AckDisposition.VALID_NOT_ADVANCING
    events = _assert_latest_transaction(connection, [29], _ACCEPTED_AT)
    ended_event_id = connection.execute(
        "SELECT ended_event_id FROM state_syncs WHERE id = 1"
    ).fetchone()[0]
    assert events[0][0] == ended_event_id
    assert connection.execute(
        "SELECT status, ack_report_id, local_report_id FROM state_syncs WHERE id = 1"
    ).fetchone() == (2, report.report_id, None)
    assert connection.execute(
        "SELECT acknowledged_wm, acknowledged_report_id FROM runtime_state"
    ).fetchone() == (report.to_wm, report.report_id)
    assert _trusted_bound(connection) == bound_before


async def test_nonadvancing_ack_without_new_facts_creates_no_history(
    environment, tmp_path
) -> None:
    connection, _ = environment
    await _accept(environment, tmp_path, _plan_body())
    await _seed_report(environment, tmp_path, report_id=1)
    report_wm, report_id = connection.execute(
        "SELECT to_wm, id FROM reports WHERE id = 1"
    ).fetchone()
    connection.execute(
        "UPDATE runtime_state SET acknowledged_wm = ?, acknowledged_report_id = ?",
        (report_wm, report_id),
    )
    history_before = _all_rows(connection, "history_events")
    transactions_before = _all_rows(connection, "history_transactions")
    bound_before = _trusted_bound(connection)

    result = await _accept(
        environment,
        tmp_path,
        _plan_body(request_id="42", ack=str(report_id)),
    )

    assert result.plan_disposition is PlanDisposition.REUSED
    assert result.ack_disposition is AckDisposition.VALID_NOT_ADVANCING
    assert _all_rows(connection, "history_events") == history_before
    assert _all_rows(connection, "history_transactions") == transactions_before
    assert _trusted_bound(connection) == bound_before
