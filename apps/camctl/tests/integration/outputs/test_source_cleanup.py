"""X8 源产物清理删除链的组件集成测试。

真实目标固定、限制建立、删除意图（operations 删除流程独立预算）、
契约替身设备调用与结果事务组合：可靠删除直接成功并同事务保存文件
缺席、产物 CLEANED 与成员终态；效果未知只能用查询预算核实；查询
确认仍在时等待预算内重试；删除预算耗尽按公共错误终态。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.outputs.cleanup_flow import (
    CleanupOutcomeChoice,
    CleanupRuntime,
    delete_source_file,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.repositories.outputs import (
    OutputsRepository,
    register_outputs_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

register_operation_guards()
register_outputs_guards()
register_capture_guards()

_NOW = 1_750_000_000_000_000
_BINDING = DeviceBinding(device_id="cam-1", driver_id="camctl-adb")

_EVIDENCE = EvidenceRegistry(
    (
        EvidenceContract(type="operation_returned", version=1, operation="delete",
                         fields=frozenset()),
        EvidenceContract(type="file_absent", version=1, operation="delete",
                         fields=frozenset({"cleanup_item_id"}),
                         identity_field="cleanup_item_id"),
        EvidenceContract(type="file_presence", version=1, operation="query",
                         fields=frozenset({"cleanup_item_id", "present"}),
                         identity_field="cleanup_item_id"),
    )
)


class DriverDouble:
    """契约替身：按编排返回删除/查询观察。"""

    def __init__(self, *, absent=None, delete_error=None, present=None,
                 query_error=None) -> None:
        self.absent = absent
        self.delete_error = delete_error
        self.present = present
        self.query_error = query_error
        self.delete_calls = 0
        self.query_calls = 0

    async def delete(self, request) -> DeviceCallResult:
        self.delete_calls += 1
        observations = ()
        if self.absent:
            observations = (
                DeviceObservation(
                    type="file_absent", version=1,
                    data={"cleanup_item_id": "91"}),
            )
        return DeviceCallResult(
            observations=observations, error=self.delete_error)

    async def query_state(self, request) -> DeviceCallResult:
        self.query_calls += 1
        observations = ()
        if self.present is not None:
            observations = (
                DeviceObservation(
                    type="file_presence", version=1,
                    data={"cleanup_item_id": "91", "present": self.present}),
            )
        return DeviceCallResult(
            observations=observations, error=self.query_error)


@pytest.fixture
def pipeline(tmp_path: Path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
        " VALUES (1, ?, 1, 1)", ("f" * 32,))
    connection.execute(
        "INSERT INTO history_events (id, transaction_id, event_type, event_version,"
        " occurred_at, clock_status, change_seq, body_json)"
        " VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})))
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (1, 4242, 'seed', ?, 1, 1, 1, 1)", (_NOW,))
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (11, 1, 0, 'rec', 2, 'cam-1', ?, NULL, '{}', '{}', 'camctl-adb',"
        " 1000, '{}', 3, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (_NOW,))
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (30, 1, 1, 'clean', 5, NULL, ?, NULL, ?, NULL, NULL, NULL, '{}',"
        " 2, 1, 0, NULL, NULL, NULL, NULL, 2, 1, 2, 1, 1, 1)",
        (_NOW, json.dumps({"params": {"output_ids": ["701"]}})))
    connection.execute(
        "INSERT INTO action_dependencies (id, action_id, depends_on_action_id)"
        " VALUES (41, 30, 11)")
    connection.execute(
        "INSERT INTO device_files (id, observer_action_id, source_action_id,"
        " identity_key, locator_json, ownership_evidence_json, role,"
        " presence_state, completion_state, completion_evidence_json,"
        " checksum_support, size_bytes, created_event_id, last_event_id,"
        " change_count) VALUES (501, 11, 11, 'file-0501', '{}', '{}', 2, 2, 3,"
        " '{}', 2, 10, 1, 1, 1)")
    connection.execute(
        "INSERT INTO outputs (id, source_action_id, kind, device_file_id,"
        " availability, cleanup_status, media_json, created_event_id,"
        " last_event_id, change_count) VALUES (701, 11, 1, 501, 1, 1, '{}',"
        " 1, 1, 1)")
    # 目标已固定：成员 91 处于未解析。
    connection.execute(
        "INSERT INTO cleanup_items (id, action_id, requested_output_id, output_id,"
        " status, restriction_state) VALUES (91, 30, 701, 701, 1, 1)")
    connection.commit()
    yield owned
    owned.connection.close()


def _runtime(owned, driver, *, delete_attempts=1, query_attempts=1) -> CleanupRuntime:
    return CleanupRuntime(
        owned=owned,
        outputs=OutputsRepository(),
        operations=OperationRepository(),
        driver=driver,
        evidence=_EVIDENCE,
        binding_of=lambda item_id: _BINDING,
        occurred_at=lambda: _NOW + 10,
        delete_config=AttemptConfig(
            max_attempts=delete_attempts, timeout_s=Decimal("10"),
            retry_interval_s=Decimal("1")),
        query_config=AttemptConfig(
            max_attempts=query_attempts, timeout_s=Decimal("10"),
            retry_interval_s=Decimal("1")),
    )


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


pytestmark = pytest.mark.asyncio


class TestDeletionChain:
    async def test_confirmed_delete_succeeds_with_cleaned_projection(self, pipeline):
        owned = pipeline
        driver = DriverDouble(absent=True)
        step = await delete_source_file(_runtime(owned, driver), 91)
        assert step.phase == "succeeded" and step.detail == "DELETED", step
        assert _value(
            owned, "SELECT status, restriction_state, outcome, error_code"
            " FROM cleanup_items WHERE id = 91") == (4, 4, 1, None)
        assert _value(
            owned, "SELECT availability, cleanup_status FROM outputs"
            " WHERE id = 701") == (3, 4)
        assert _value(
            owned, "SELECT presence_state FROM device_files WHERE id = 501") == (3,)
        # 删除流程一次尝试；查询流程未动。
        assert driver.delete_calls == 1 and driver.query_calls == 0
        assert _value(
            owned, "SELECT COUNT(*) FROM operation_runs"
            " WHERE responsibility_key = 'delete/91'") == (1,)

    async def test_unknown_delete_confirmed_absent_by_query(self, pipeline):
        owned = pipeline
        driver = DriverDouble(absent=False, present=False)
        step = await delete_source_file(_runtime(owned, driver), 91)
        assert step.phase == "succeeded" and step.detail == "ABSENCE_CONFIRMED", step
        assert _value(
            owned, "SELECT status, outcome FROM cleanup_items WHERE id = 91"
        ) == (4, 3)
        assert driver.delete_calls == 1 and driver.query_calls == 1
        # 两个流程各自一次尝试：预算独立。
        assert _value(
            owned, "SELECT COUNT(*) FROM operation_runs"
            " WHERE responsibility_key IN ('delete/91', 'exists/91')") == (2,)

    async def test_unknown_delete_with_file_still_present_waits(self, pipeline):
        owned = pipeline
        driver = DriverDouble(absent=False, present=True)
        step = await delete_source_file(
            _runtime(owned, driver, delete_attempts=2), 91)
        assert step.phase == "still_present", step
        # 成员保持删除中，不因文件仍在判定失败。
        assert _value(
            owned, "SELECT status FROM cleanup_items WHERE id = 91") == (3,)
        # 预算内重试：再次推进发起第二次删除并成功。
        driver.absent = True
        retry = await delete_source_file(
            _runtime(owned, driver, delete_attempts=2), 91)
        assert retry.phase == "succeeded", retry
        assert driver.delete_calls == 2

    async def test_delete_budget_exhaustion_fails_with_registered_error(
            self, pipeline):
        owned = pipeline
        driver = DriverDouble(absent=False, present=True)
        # 第一次：删除效果未知，查询确认仍在——等待预算内重试。
        first = await delete_source_file(
            _runtime(owned, driver, delete_attempts=1, query_attempts=1), 91)
        assert first.phase == "still_present", first
        # 重试进入删除：删除预算耗尽按公共错误终态失败；查询预算独立
        # 不受影响（未确认缺席不以删除代替核实）。
        second = await delete_source_file(
            _runtime(owned, driver, delete_attempts=1, query_attempts=1), 91)
        assert second.phase == "failed", second
        assert second.detail == "delete_attempts_exhausted", second
        row = _value(
            owned, "SELECT status, error_code, error_details_json"
            " FROM cleanup_items WHERE id = 91")
        assert row[0] == 5
        assert row[1] == 2
        assert json.loads(row[2])["output_id"] == "701"

    async def test_terminal_member_is_idempotent(self, pipeline):
        owned = pipeline
        driver = DriverDouble(absent=True)
        await delete_source_file(_runtime(owned, driver), 91)
        again = await delete_source_file(_runtime(owned, driver), 91)
        assert again.phase == "already_terminal", again
        assert driver.delete_calls == 1
        assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
