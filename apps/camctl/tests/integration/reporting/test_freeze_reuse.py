"""原冻结键返回原响应，不受后续机会或报告管理状态影响。"""

from contextlib import closing
from dataclasses import replace
import json
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError, OperationKey, new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.reporting import policy
from camctl.reporting.models import ReportBytes
from camctl.reporting.policy import ReportingRepository, ReportDecisionKind, record_report_bytes

from .test_freeze import environment, _submit, _ack, _sync
from ..persistence.test_transactions import FailingConnection

pytestmark = pytest.mark.asyncio


def _freeze(owned, key, occurred_at=1):
    return ReportingRepository().freeze_report(key, owned, occurred_at=occurred_at)


def _readonly(owned, key, occurred_at=1):
    before = tuple(owned.connection.iterdump())
    result = _freeze(owned, key, occurred_at)
    assert tuple(owned.connection.iterdump()) == before
    return result


@pytest.mark.parametrize("later", ["none", "submission", "ack", "newer_report", "sync_end", "bytes"])
async def test_original_freeze_key_returns_original_generate_response(environment, tmp_path, later):
    await _submit(environment, tmp_path, "1")
    key = new_operation_key()
    first = _freeze(environment, key)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert first.value.kind is ReportDecisionKind.GENERATE
    if later in ("submission", "newer_report"):
        await _submit(environment, tmp_path, "2")
        if later == "newer_report":
            assert _freeze(environment, new_operation_key()).value.report.report_id != first.value.report.report_id
    elif later == "ack":
        _ack(environment, first.value.report)
    elif later == "sync_end":
        # 有效同步投影由既有夹具提供；实际开始生产者另有门禁。
        _sync(environment.connection, 1)
        _ack(environment, first.value.report)
    elif later == "bytes":
        prepared = record_report_bytes(new_operation_key(), environment, first.value.report.report_id,
                                      ReportBytes(5, "a" * 64), occurred_at=2)
        assert prepared.kind is DbOutcomeKind.COMPLETED, prepared.error
    result = _readonly(environment, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value == first.value


async def test_original_freeze_is_resolved_before_current_opportunity(environment, tmp_path, monkeypatch):
    await _submit(environment, tmp_path, "1")
    key = new_operation_key()
    first = _freeze(environment, key)
    monkeypatch.setattr(policy, "read_report_opportunity", create_autospec(
        policy.read_report_opportunity, side_effect=AssertionError("原冻结不能重新选择当前机会")))
    result = _readonly(environment, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value == first.value


@pytest.mark.parametrize("phase", ["input", "prepare"])
async def test_freeze_rejects_key_owned_by_another_phase(environment, tmp_path, phase):
    await _submit(environment, tmp_path, "1")
    if phase == "input":
        with closing(environment.connection.execute("SELECT operation_key FROM history_transactions WHERE id = 1")) as cursor:
            key = OperationKey(cursor.fetchone()[0])
    else:
        report = _freeze(environment, new_operation_key()).value.report
        key = new_operation_key()
        assert record_report_bytes(key, environment, report.report_id, ReportBytes(5, "a" * 64)).kind is DbOutcomeKind.COMPLETED
    result = _readonly(environment, key, occurred_at=0)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.mark.parametrize("occurred_at", [2, True, 1.0])
async def test_freeze_rejects_changed_or_invalid_original_time(environment, tmp_path, occurred_at):
    await _submit(environment, tmp_path, "1")
    key = new_operation_key()
    _freeze(environment, key)
    result = _readonly(environment, key, occurred_at)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ValueError), result.error


@pytest.mark.parametrize("mutation", ["missing", "created", "frozen", "from", "to", "version", "h_member"])
async def test_freeze_rejects_unreliable_original_definition(environment, tmp_path, mutation):
    await _submit(environment, tmp_path, "1")
    key = new_operation_key()
    report = _freeze(environment, key).value.report
    connection = environment.connection
    if mutation == "missing":
        connection.execute("DELETE FROM reports WHERE id = ?", (report.report_id,))
    elif mutation == "h_member":
        connection.execute("DELETE FROM history_events WHERE id = ?", (report.boundary.last_event_id,))
    elif mutation == "created":
        prepared = record_report_bytes(new_operation_key(), environment, report.report_id, ReportBytes(5, "a" * 64))
        assert prepared.kind is DbOutcomeKind.COMPLETED, prepared.error
        connection.execute("UPDATE reports SET created_event_id = last_event_id WHERE id = ?", (report.report_id,))
    elif mutation == "version":
        with closing(connection.execute("SELECT created_event_id FROM reports WHERE id = ?", (report.report_id,))) as cursor:
            event_id = cursor.fetchone()[0]
        with closing(connection.execute("SELECT body_json FROM history_events WHERE id = ?", (event_id,))) as cursor:
            body = json.loads(cursor.fetchone()[0])
        body["rows"][0]["after"]["values"]["format_version"] = 2
        connection.execute("UPDATE history_events SET body_json = ? WHERE id = ?", (json.dumps(body), event_id))
    else:
        column, value = {"frozen": ("frozen_event_id", 1), "from": ("from_wm", 1),
                         "to": ("to_wm", 1)}[mutation]
        connection.execute(f"UPDATE reports SET {column} = ? WHERE id = ?", (value, report.report_id))
    result = _readonly(environment, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.mark.parametrize("invalid", ["range", "body", "missing_member", "composite"])
async def test_freeze_rejects_invalid_or_composite_original_transaction(environment, tmp_path, invalid):
    await _submit(environment, tmp_path, "1")
    key = new_operation_key()
    report = _freeze(environment, key).value.report
    connection = environment.connection
    with closing(connection.execute("SELECT id, last_event_id FROM history_transactions WHERE operation_key = ?", (str(key),))) as cursor:
        txn_id, event_id = cursor.fetchone()
    if invalid == "range":
        connection.execute("UPDATE history_transactions SET last_event_id = last_event_id + 1 WHERE id = ?", (txn_id,))
    elif invalid == "body":
        connection.execute("UPDATE history_events SET body_json = '{}' WHERE id = ?", (event_id,))
    elif invalid == "missing_member":
        connection.execute("DELETE FROM history_events WHERE id = ?", (event_id,))
    else:
        prepared = record_report_bytes(new_operation_key(), environment, report.report_id, ReportBytes(5, "a" * 64))
        assert prepared.kind is DbOutcomeKind.COMPLETED, prepared.error
        connection.execute("UPDATE history_events SET transaction_id = ? WHERE id = ?", (txn_id, event_id + 1))
        connection.execute("DELETE FROM history_transactions WHERE id = ?", (txn_id + 1,))
        connection.execute("UPDATE history_transactions SET last_event_id = ? WHERE id = ?", (event_id + 1, txn_id))
    result = _readonly(environment, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.mark.parametrize("column", ["from_wm", "to_wm"])
async def test_original_freeze_rejects_matching_but_invalid_business_range(environment, tmp_path, column):
    await _submit(environment, tmp_path, "1")
    key = new_operation_key()
    report = _freeze(environment, key).value.report
    connection = environment.connection
    with closing(connection.execute("SELECT created_event_id FROM reports WHERE id = ?", (report.report_id,))) as cursor:
        event_id = cursor.fetchone()[0]
    with closing(connection.execute("SELECT body_json FROM history_events WHERE id = ?", (event_id,))) as cursor:
        body = json.loads(cursor.fetchone()[0])
    body["rows"][0]["after"]["values"][column] = 1
    connection.execute("UPDATE history_events SET body_json = ? WHERE id = ?", (json.dumps(body), event_id))
    connection.execute(f"UPDATE reports SET {column} = 1 WHERE id = ?", (report.report_id,))
    result = _readonly(environment, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


async def test_original_freeze_preserves_nonzero_business_boundary(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    _ack(environment, _freeze(environment, new_operation_key()).value.report)
    await _submit(environment, tmp_path, "2")
    key = new_operation_key()
    first = _freeze(environment, key)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert first.value.report.from_wm == 2
    assert first.value.report.to_wm == 4
    result = _readonly(environment, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value == first.value


@pytest.mark.parametrize("prefix", ["SELECT id FROM history_transactions", "SELECT id, transaction_id",
                                  "SELECT from_wm", "SELECT created_event_id", "SELECT MAX(change_seq)",
                                  "SELECT event.id FROM history_events AS event"])
async def test_original_freeze_query_failure_preserves_database(environment, tmp_path, prefix):
    await _submit(environment, tmp_path, "1")
    _ack(environment, _freeze(environment, new_operation_key()).value.report)
    await _submit(environment, tmp_path, "2")
    key = new_operation_key()
    _freeze(environment, key)
    before = tuple(environment.connection.iterdump())
    fault = FailingConnection(environment.connection, prefix)
    result = _freeze(replace(environment, connection=fault), key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, sqlite3.OperationalError), result.error
    assert tuple(environment.connection.iterdump()) == before


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_commit_is_resolved_with_same_freeze_key(environment, tmp_path, committed):
    await _submit(environment, tmp_path, "1")
    key = new_operation_key()
    connection = environment.connection

    class LostReceipt:
        def execute(self, sql, parameters=()):
            if sql == "COMMIT":
                if committed:
                    connection.execute(sql, parameters)
                raise sqlite3.OperationalError("没有取得提交收据")
            return connection.execute(sql, parameters)

    result = _freeze(replace(environment, connection=LostReceipt()), key)
    assert result.kind is DbOutcomeKind.UNKNOWN
    if connection.in_transaction:
        connection.rollback()
    await _submit(environment, tmp_path, "2")
    retry = _readonly(environment, key) if committed else _freeze(environment, key)
    assert retry.kind is DbOutcomeKind.COMPLETED, retry.error
    assert retry.value.kind is ReportDecisionKind.GENERATE
    assert retry.value.report.to_wm == (2 if committed else 4)
    with closing(connection.execute("SELECT COUNT(*) FROM reports")) as cursor:
        assert cursor.fetchone()[0] == 1


@pytest.mark.parametrize("first_kind", [ReportDecisionKind.REUSE, ReportDecisionKind.SKIP])
async def test_unsaved_readonly_decision_does_not_bind_future_freeze(environment, tmp_path, first_kind):
    if first_kind is ReportDecisionKind.REUSE:
        await _submit(environment, tmp_path, "1")
        _freeze(environment, new_operation_key())
    key = new_operation_key()
    first = _readonly(environment, key)
    assert first.value.kind is first_kind
    with closing(environment.connection.execute("SELECT id FROM history_transactions WHERE operation_key = ?", (str(key),))) as cursor:
        assert cursor.fetchone() is None
    await _submit(environment, tmp_path, "2")
    current = _freeze(environment, key)
    assert current.kind is DbOutcomeKind.COMPLETED, current.error
    assert current.value.kind is ReportDecisionKind.GENERATE
