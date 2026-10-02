"""读取建档通过真实事务保存精确数字，失败不留下部分责任。"""

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import sqlite3

import pytest

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import new_operation_key
from camctl.outputs.qualification import FileCandidate, OperationConfig, QualificationOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import OutputsRepository

from ..operations.test_result_reuse import _FaultConnection
from .test_qualification import (
    _NOW, _seed_action, _seed_device_file, _seed_environment, _seed_output,
    _seed_plan, _seed_processing,
)


@pytest.fixture
def internal_read(tmp_path):
    _, owned = _seed_environment(tmp_path)
    try:
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        _seed_action(connection, 11, 1, action_type=2)
        _seed_device_file(connection, 501, 11)
        _seed_output(connection, 701, 11, 501)
        _seed_processing(connection, 5, 11, 501)
        connection.execute(
            "UPDATE recording_processing SET check_decision=3, check_basis_json='{}' WHERE id=5"
        )
        connection.commit()
        register_operation_guards()
        command = FileCandidate(
            action_id=11, item_id=None, processing_id=5, output_id=None,
            source_device_file_id=501, target_relative_path="recording-inputs/1.part",
            delivery_file_name="1.mp4", delivery_display_name="原片检查",
            config=OperationConfig(3, Decimal("10"), Decimal("3")), occurred_at=_NOW,
        )
        yield owned, command
    finally:
        owned.connection.close()


@pytest.mark.parametrize("timeout,interval", [
    ("10", "0"),
    ("1.234567890123456789", "0.0000000000000000001"),
])
def test_read_creation_saves_exact_numeric_config_without_attempt(internal_read, timeout, interval):
    owned, command = internal_read
    command = replace(command, config=OperationConfig(3, Decimal(timeout), Decimal(interval)))
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.GRANTED
    with closing(owned.connection.execute(
        "SELECT max_attempts_used, timeout_s_json, retry_interval_s_json,"
        " json_type(timeout_s_json), json_type(retry_interval_s_json), attempts_used"
        " FROM operation_runs WHERE id=?", (result.value.run_id,),
    )) as cursor:
        attempts, saved_timeout, saved_interval, timeout_type, interval_type, used = cursor.fetchone()
    assert attempts == 3
    assert used == 0
    assert timeout_type in ("integer", "real")
    assert interval_type in ("integer", "real")
    assert parse_exact_json(saved_timeout) == Decimal(timeout)
    assert parse_exact_json(saved_interval) == Decimal(interval)
    with closing(owned.connection.execute(
        "SELECT body_json FROM history_events WHERE event_type=10",
    )) as cursor:
        body = parse_exact_json(cursor.fetchone()[0])
    saved_config = body["rows"][0]["after"]["values"]
    assert saved_config["timeout_s_json"] == Decimal(timeout)
    assert saved_config["retry_interval_s_json"] == Decimal(interval)
    with closing(owned.connection.execute("SELECT count(*) FROM operation_attempts")) as cursor:
        assert cursor.fetchone() == (0,)


@pytest.mark.parametrize("prefix", [
    "INSERT INTO intermediate_files", "INSERT INTO operation_runs", "INSERT INTO file_copies",
])
def test_read_creation_write_failure_rolls_back_complete_responsibility(internal_read, prefix):
    owned, command = internal_read
    before = tuple(owned.connection.iterdump())
    failing = replace(owned, connection=_FaultConnection(owned.connection, prefix))
    key = new_operation_key()
    result = OutputsRepository().grant_file(command, key, failing)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, sqlite3.OperationalError), result.error
    assert tuple(owned.connection.iterdump()) == before
    assert owned.connection.in_transaction is False
    retry = OutputsRepository().grant_file(command, key, owned)
    assert retry.kind is DbOutcomeKind.COMPLETED, retry.error
