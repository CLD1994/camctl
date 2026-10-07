"""X9 清理取消、接手与原结果保持的组件集成测试。

真实仓储、操作预算与契约替身组合：请求 A 查询预算耗尽失败后保持
原结果，请求 B 以自己的查询预算接手并确认缺席成功；取消未发出删
除的成员解除限制；已发出且效果未知的删除保留不可撤销限制并按
delete_unconfirmed 收场；在途调用跟踪到实际结束后保存。
"""

from __future__ import annotations

import asyncio
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
    CleanupRuntime,
    FixCleanupTargets,
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
        EvidenceContract(type="delete_returned", version=1, operation="delete",
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
    """契约替身：按编排返回删除/查询观察；观察身份取所属成员。"""

    def __init__(self, *, absent=None, delete_error=None, present=None,
                 query_error=None, item="91") -> None:
        self.absent = absent
        self.delete_error = delete_error
        self.present = present
        self.query_error = query_error
        self.item = item
        self.delete_calls = 0
        self.query_calls = 0

    async def delete(self, request) -> DeviceCallResult:
        self.delete_calls += 1
        observations = ()
        if self.absent:
            observations = (
                DeviceObservation(
                    type="file_absent", version=1,
                    data={"cleanup_item_id": self.item}),
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
                    data={"cleanup_item_id": self.item,
                          "present": self.present}),
            )
        return DeviceCallResult(
            observations=observations, error=self.query_error)


class GatedDriver:
    """在途调用替身：删除调用挂起直到放行，模拟取消后到场的返回。"""

    def __init__(self, *, release: asyncio.Event) -> None:
        self.release = release
        self.delete_calls = 0
        self.query_calls = 0

    async def delete(self, request) -> DeviceCallResult:
        self.delete_calls += 1
        await self.release.wait()
        # 返回无观察成功：删除效果未知。
        return DeviceCallResult(observations=(), error=None)

    async def query_state(self, request) -> DeviceCallResult:
        self.query_calls += 1
        return DeviceCallResult(observations=(), error=object())


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
    for action_id, index, name, pending in (
            (30, 1, "clean-a", 2), (31, 2, "clean-b", 1)):
        connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
            " scheduled_at, group_name, input_fields_json, effective_params_json,"
            " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
            " cancel_requested, error_code, error_details_json,"
            " first_window_observed_at, expiration_reason, source_resolution_state,"
            " resolved_source_plan_id, target_selection_state, created_event_id,"
            " last_event_id, change_count)"
            " VALUES (?, 1, ?, ?, 5, NULL, ?, NULL, ?, NULL, NULL, NULL, '{}',"
            " 2, 1, 0, NULL, NULL, NULL, NULL, 2, 1, ?, 1, 1, 1)",
            (action_id, index, name, _NOW,
             json.dumps({"params": {"output_ids": ["701"]}}), pending))
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
    # 两个清理动作的目标均已固定：成员 91（A）与 92（B）处于未解析。
    connection.execute(
        "INSERT INTO cleanup_items (id, action_id, requested_output_id, output_id,"
        " status, restriction_state) VALUES (91, 30, 701, 701, 1, 1)")
    connection.commit()
    yield owned
    owned.connection.close()


def _runtime(owned, driver, *, delete_attempts=1, query_attempts=1,
             tick=_NOW + 10) -> CleanupRuntime:
    return CleanupRuntime(
        owned=owned,
        outputs=OutputsRepository(),
        operations=OperationRepository(),
        driver=driver,
        evidence=_EVIDENCE,
        binding_of=lambda item_id: _BINDING,
        occurred_at=lambda: tick,
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


def _seed_pending_delete(owned, item_id: int = 91) -> None:
    """成员已建立限制并待删除，产物投影受限且待清理。"""
    owned.connection.execute(
        "UPDATE cleanup_items SET status = 2, restriction_state = 2 WHERE id = ?",
        (item_id,))
    owned.connection.execute(
        "UPDATE outputs SET availability = 2, cleanup_status = 2 WHERE id = 701")
    owned.connection.commit()


def _fix_b(owned):
    from camctl.persistence.repositories.outputs import OutputsRepository

    outcome = OutputsRepository().fix_cleanup_targets(
        FixCleanupTargets(31, _NOW + 5), new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return outcome.value


pytestmark = pytest.mark.asyncio


class TestTakeoverPreservesOldFailure:
    async def test_later_cleanup_preserves_old_failure(self, pipeline):
        owned = pipeline
        # A：删除效果未知且查询预算耗尽——最终失败并保留限制。
        driver = DriverDouble(absent=False, query_error=object())
        first = await delete_source_file(
            _runtime(owned, driver, delete_attempts=2, query_attempts=1), 91)
        assert first.phase == "query_unknown", first
        second = await delete_source_file(
            _runtime(owned, driver, delete_attempts=2, query_attempts=1), 91)
        assert second.phase == "failed", second
        assert second.detail == "file_query_attempts_exhausted", second
        row = _value(
            owned, "SELECT status, restriction_state, error_code, error_details_json"
            " FROM cleanup_items WHERE id = 91")
        assert row[:3] == (5, 4, 3)
        assert json.loads(row[3])["output_id"] == "701"
        # 全部待处理责任结束：产物汇总进入未完成并保留 A 的结束原因。
        output = _value(
            owned, "SELECT availability, cleanup_status, cleanup_error_json"
            " FROM outputs WHERE id = 701")
        assert output[:2] == (2, 5)
        assert json.loads(output[2])["code"] == "file_query_attempts_exhausted"

        # B：后来的有效请求接手同一产物，不重开 A 的旧项。
        b_item = _fix_b(owned).item_ids[0]
        driver_b = DriverDouble(absent=False, present=False, item=str(b_item))
        # 接手前置：A 留下效果未知的删除，B 先用自己的查询预算核实。
        taken = await delete_source_file(_runtime(owned, driver_b), b_item)
        assert taken.phase == "succeeded", taken
        assert taken.detail == "ABSENCE_CONFIRMED", taken
        assert driver_b.delete_calls == 0
        assert driver_b.query_calls == 1
        # A 的原失败保持不变；B 保存自己的成功。
        assert _value(
            owned, "SELECT status, error_code FROM cleanup_items WHERE id = 91"
        ) == (5, 3)
        assert _value(
            owned, "SELECT status, outcome FROM cleanup_items WHERE action_id = 31"
        ) == (4, 3)
        assert _value(
            owned, "SELECT availability, cleanup_status, cleanup_error_json"
            " FROM outputs WHERE id = 701") == (3, 4, None)


class TestCleanupCancel:
    async def test_cancel_before_delete_releases_restriction(self, pipeline):
        owned = pipeline
        _seed_pending_delete(owned)
        owned.connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 30")
        owned.connection.commit()
        driver = DriverDouble(absent=True)
        step = await delete_source_file(_runtime(owned, driver), 91)
        assert step.phase == "canceled", step
        assert driver.delete_calls == 0
        assert _value(
            owned, "SELECT status, restriction_state, error_code"
            " FROM cleanup_items WHERE id = 91") == (6, 3, None)
        # 曾建立删除意图并在允许阶段解除：汇总取消，可用性按文件事实恢复。
        assert _value(
            owned, "SELECT availability, cleanup_status, cleanup_error_json"
            " FROM outputs WHERE id = 701") == (1, 6, None)

    async def test_cancel_of_unresolved_member_keeps_no_restriction(self, pipeline):
        owned = pipeline
        owned.connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 30")
        owned.connection.commit()
        driver = DriverDouble(absent=True)
        step = await delete_source_file(_runtime(owned, driver), 91)
        assert step.phase == "canceled", step
        assert driver.delete_calls == 0
        assert _value(
            owned, "SELECT status, restriction_state, error_code"
            " FROM cleanup_items WHERE id = 91") == (6, 1, None)
        # 从未建立有效删除意图：汇总回到未请求，无投影变化事件要求。
        assert _value(
            owned, "SELECT availability, cleanup_status FROM outputs"
            " WHERE id = 701") == (1, 1)

    async def test_cancel_with_unknown_delete_keeps_restriction(self, pipeline):
        owned = pipeline
        driver = DriverDouble(absent=False, query_error=object())
        first = await delete_source_file(_runtime(owned, driver), 91)
        assert first.phase == "query_unknown", first
        owned.connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 30")
        owned.connection.commit()
        settled = await delete_source_file(_runtime(owned, driver), 91)
        assert settled.phase == "canceled", settled
        assert settled.detail == "delete_unconfirmed", settled
        row = _value(
            owned, "SELECT status, restriction_state, error_code, error_details_json"
            " FROM cleanup_items WHERE id = 91")
        assert row[:3] == (6, 4, 4)
        assert json.loads(row[3]) == {"output_id": "701"}
        output = _value(
            owned, "SELECT availability, cleanup_status, cleanup_error_json"
            " FROM outputs WHERE id = 701")
        assert output[:2] == (2, 5)
        assert json.loads(output[2])["code"] == "delete_unconfirmed"

    async def test_cancel_with_confirmed_presence_reports_delete_failed(
            self, pipeline):
        owned = pipeline
        driver = DriverDouble(absent=False, present=True)
        first = await delete_source_file(
            _runtime(owned, driver, delete_attempts=2), 91)
        assert first.phase == "still_present", first
        owned.connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 30")
        owned.connection.commit()
        settled = await delete_source_file(
            _runtime(owned, driver, delete_attempts=2), 91)
        assert settled.phase == "canceled", settled
        assert settled.detail == "file_delete_failed", settled
        assert _value(
            owned, "SELECT status, restriction_state, error_code"
            " FROM cleanup_items WHERE id = 91") == (6, 4, 6)

    async def test_in_flight_call_tracked_to_settlement(self, pipeline):
        owned = pipeline
        release = asyncio.Event()
        driver = GatedDriver(release=release)
        task = asyncio.ensure_future(
            delete_source_file(_runtime(owned, driver), 91))
        # 等待流程推进到删除调用挂起后，取消才到达。
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        owned.connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 30")
        owned.connection.commit()
        release.set()
        step = await task
        assert step.phase == "canceled", step
        assert step.detail == "delete_unconfirmed", step
        # 在途调用的结束事实已保存：删除尝试存在且不在执行中。
        assert _value(
            owned, "SELECT COUNT(*) FROM operation_attempts a"
            " JOIN operation_runs r ON a.run_id = r.id"
            " WHERE r.responsibility_key = 'delete/91' AND a.status <> 1"
        ) == (1,)
        assert _value(
            owned, "SELECT status, restriction_state, error_code"
            " FROM cleanup_items WHERE id = 91") == (6, 4, 4)
