"""固定 H 的报告入选选择与对象恢复（真实生产链种子）。

按业务水位窗口选择报告目标并沿报告实体登记的外键补齐父对象；
对象恢复在同一短读事务内联合读取当前投影与 C，逆向恢复到 H；
H 后创建的行不进入报告事实，分页大小不改变入选集合与字节。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.capture.dispatch import dispatch_ready, ready_capture_actions
from camctl.capture.handlers import CaptureRuntime
from camctl.capture.results import FileKind as ResultFileKind
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import new_operation_key
from camctl.history.queries import ReportScopeRequest, build_report_scope
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    register_capture_guards,
)
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
from camctl.persistence.repositories.timelapse import (
    TimelapseRepository,
    register_timelapse_guards,
)
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting.policy import (
    register_report_guards,
    register_sync_guard,
)
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.cancellation import register_cancellation_guards
from camctl.scheduling.rules import LaunchWindow

from ..capture.test_capture_contract import (
    DriverDouble,
    ResultsDouble,
    _PAGE_EVIDENCE,
    _entry,
)
from ..persistence.test_runtime import _create_valid_database

register_acceptance_guards()
register_operation_guards()
register_capture_guards()
register_timelapse_guards()
register_report_guards()
register_sync_guard()
register_outputs_guards()
register_cancellation_guards()

_CAMERA_DEFINITION = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"type": {"const": "single_shot"}},
    "required": ["type"],
    "additionalProperties": False,
}


class _Catalog:
    """受理目录替身：支持 cam-1 的单张拍摄。"""

    def action_types(self):
        return frozenset({"camera_take_photo"})

    def device_exists(self, device_id):
        return device_id == "cam-1"

    def driver_id(self, device_id):
        return "camctl-adb" if device_id == "cam-1" else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type == "camera_take_photo"

    def parameter_definition(self, device_id, action_type, parameter_type):
        if not self.device_supports(device_id, action_type):
            return None
        return ParameterDefinition(schema=_CAMERA_DEFINITION, defaults={})


def _past_schedule() -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=1)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _plan_body(request_id: str, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at,
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            }
        ],
    }


class _Reader:
    def read(self, path: str) -> bytes:
        return Path(path).read_bytes()


async def _submit(owned, tmp_path, request_id: str, scheduled_at: str | None = None):
    body = _plan_body(request_id, scheduled_at or _past_schedule())
    target = tmp_path / f"plan-{request_id}.json"
    target.write_text(json.dumps(body), encoding="utf-8")
    result = await accept_input(
        parse_input(await read_input(str(target), _Reader())),
        AcceptanceContext(
            mode=CommandMode.RUN,
            catalog=_Catalog(),
            repository=AcceptanceRepository(),
            clock=type("C", (), {"utc_micros": staticmethod(lambda: 1)})(),
        ),
        new_operation_key(),
        owned,
    )
    assert result.plan_id is not None


async def _submit_diagnostic(owned, tmp_path, name: str):
    target = tmp_path / f"{name}.json"
    target.write_text(json.dumps({"request_id": f"bad-{name}", "plan": {}}), encoding="utf-8")
    await accept_input(
        parse_input(await read_input(str(target), _Reader())),
        AcceptanceContext(
            mode=CommandMode.RUN,
            catalog=_Catalog(),
            repository=AcceptanceRepository(),
            clock=type("C", (), {"utc_micros": staticmethod(lambda: 1)})(),
        ),
        new_operation_key(),
        owned,
    )


class _ActivityDriver(DriverDouble):
    """契约替身：观察身份使用真实活动身份。"""

    def __init__(self, activity_target: str = "1", error=None) -> None:
        super().__init__(error=error)
        self._target = activity_target

    async def control(self, request) -> "DeviceCallResult":
        from camctl.devices.evidence import DeviceObservation
        from camctl.devices.ports import DeviceCallResult

        self.calls.append(request.operation)
        observation_type, _ = self._OBSERVATIONS[request.operation]
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type=observation_type, version=1,
                    data={"activity_id": self._target}),
            ),
            error=self.error,
        )


def _runtime(owned) -> CaptureRuntime:
    return CaptureRuntime(
        owned=owned,
        scheduling=SchedulingRepository(),
        operations=OperationRepository(),
        capture=CaptureRepository(),
        timelapse=TimelapseRepository(),
        driver=_ActivityDriver(activity_target="1"),
        results=ResultsDouble({1: (_entry("shot-1", kind=ResultFileKind.PHOTO),)}),
        evidence=_PAGE_EVIDENCE,
        wall_us=lambda: int(time.time() * 1_000_000),
        monotonic_ns=time.monotonic_ns,
        window_of=lambda action: LaunchWindow(
            scheduled_at=action["scheduled_at"],
            window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
        wait_config=lambda params: CaptureWaitConfig(
            target_duration_ms=600_000, driver_margin_ms=0),
    )


async def _finish_action(owned) -> None:
    """把已受理的到期单张动作推进到成功终态并登记正式产物。"""
    now = int(time.time() * 1_000_000)
    scheduling = SchedulingRepository()
    outcome = scheduling.start_action(
        StartActionRequest(action_id=1, trusted_wall_now=now, occurred_at=now),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    descriptors = ready_capture_actions(owned.connection, now)
    await dispatch_ready(_runtime(owned), descriptors)


def _boundary(connection) -> HistoryBoundary:
    row = connection.execute(
        "SELECT id, last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return HistoryBoundary(int(row[0]), int(row[1]))


def _to_wm(connection) -> int:
    return int(connection.execute(
        "SELECT COALESCE(MAX(change_seq), 0) FROM history_events").fetchone()[0])


@pytest.fixture
def finished_photo(tmp_path):
    """真实受理并完成一个单张拍摄计划；返回库路径。"""
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

    _create_valid_database(tmp_path / "state.db")
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        yield owned, tmp_path
    finally:
        owned.connection.close()


@pytest.mark.asyncio
class TestSelectReportScope:
    async def test_window_selects_targets_and_completes_parents(
        self, finished_photo
    ) -> None:
        owned, tmp_path = finished_photo
        await _submit(owned, tmp_path, "1")
        accept_boundary = _boundary(owned.connection)
        accept_wm = _to_wm(owned.connection)
        await _finish_action(owned)
        finish_wm = _to_wm(owned.connection)

        repository = HistoryRepository(tmp_path / "state.db")
        scope = repository.select_report_scope(ReportScopeRequest(
            boundary=accept_boundary, from_wm=accept_wm, to_wm=finish_wm,
            entity_batch_size=64))
        # 产物入选后补齐动作与计划；入选树逐层限定。
        assert scope.plan_ids == (1,)
        assert scope.action_ids_of(1) == (1,)
        assert scope.output_ids_of(1, 1) == (1,)
        assert scope.diagnostics == ()

    async def test_scope_ignores_changes_outside_window(self, finished_photo) -> None:
        owned, tmp_path = finished_photo
        await _submit(owned, tmp_path, "1")
        boundary = _boundary(owned.connection)
        to_wm = _to_wm(owned.connection)
        await _finish_action(owned)
        # 完成阶段的变化落在窗口之外：动作自受理即公开入选，但产物
        # 在窗口外创建，不进入本报告。
        repository = HistoryRepository(tmp_path / "state.db")
        scope = repository.select_report_scope(ReportScopeRequest(
            boundary=boundary, from_wm=0, to_wm=to_wm, entity_batch_size=64))
        assert scope.plan_ids == (1,)
        assert scope.action_ids_of(1) == (1,)
        assert scope.output_ids_of(1, 1) == ()
        assert scope.diagnostics == ()

    async def test_diagnostics_in_window_are_selected(self, finished_photo) -> None:
        owned, tmp_path = finished_photo
        await _submit_diagnostic(owned, tmp_path, "d1")
        boundary = _boundary(owned.connection)
        to_wm = _to_wm(owned.connection)
        repository = HistoryRepository(tmp_path / "state.db")
        scope = repository.select_report_scope(ReportScopeRequest(
            boundary=boundary, from_wm=0, to_wm=to_wm, entity_batch_size=64))
        assert scope.plan_ids == ()
        assert scope.diagnostics == (1,)

    async def test_batch_size_does_not_change_scope(self, finished_photo) -> None:
        owned, tmp_path = finished_photo
        await _submit(owned, tmp_path, "1")
        await _finish_action(owned)
        boundary = _boundary(owned.connection)
        to_wm = _to_wm(owned.connection)
        repository = HistoryRepository(tmp_path / "state.db")
        small = repository.select_report_scope(ReportScopeRequest(
            boundary=boundary, from_wm=0, to_wm=to_wm, entity_batch_size=1))
        large = repository.select_report_scope(ReportScopeRequest(
            boundary=boundary, from_wm=0, to_wm=to_wm, entity_batch_size=64))
        assert small == large


@pytest.mark.asyncio
class TestRestoreEntity:
    async def test_rows_restore_to_boundary_h(self, finished_photo) -> None:
        owned, tmp_path = finished_photo
        await _submit(owned, tmp_path, "1")
        boundary = _boundary(owned.connection)
        await _finish_action(owned)

        repository = HistoryRepository(tmp_path / "state.db")
        plan_rows = repository.restore_entity("plan", 1, boundary)
        plan = plan_rows[("plans", 1)]
        assert plan["status"] == 1  # 受理后仍待执行；当前投影已是完成
        action_rows = repository.restore_entity("action", 1, boundary)
        assert action_rows[("actions", 1)]["status"] == 1
        assert action_rows[("actions", 1)]["execution_started"] == 0

    async def test_rows_created_after_h_are_excluded(self, finished_photo) -> None:
        owned, tmp_path = finished_photo
        await _submit(owned, tmp_path, "1")
        boundary = _boundary(owned.connection)
        await _finish_action(owned)

        repository = HistoryRepository(tmp_path / "state.db")
        action_rows = repository.restore_entity("action", 1, boundary)
        # 设备活动与产物在 H 之后创建：不进入 H 的事实。
        assert ("device_activities", 1) not in action_rows
        output_rows = repository.restore_entity("output", 1, boundary)
        assert output_rows == {}

    async def test_restored_rows_match_h_snapshot(self, finished_photo) -> None:
        owned, tmp_path = finished_photo
        await _submit(owned, tmp_path, "1")
        await _finish_action(owned)
        boundary = _boundary(owned.connection)
        # H 之后的无关变化不改变已提交边界的内容。
        await _submit(owned, tmp_path, "2")

        repository = HistoryRepository(tmp_path / "state.db")
        action_rows = repository.restore_entity("action", 1, boundary)
        assert action_rows[("actions", 1)]["status"] == 3
        assert ("device_activities", 1) in action_rows
        plan_rows = repository.restore_entity("plan", 1, boundary)
        assert plan_rows[("plans", 1)]["status"] == 3


class TestBuildReportScope:
    def test_missing_parent_fact_is_consistency_error(self) -> None:
        from camctl.contracts.values import ConsistencyError

        with pytest.raises(ConsistencyError):
            build_report_scope(
                plans=(), actions=(), outputs=(9,), deliveries=(),
                diagnostics=(), action_plan={}, output_action={}, delivery_action={},
            )

    def test_selected_action_completes_its_plan(self) -> None:
        scope = build_report_scope(
            plans=(2,), actions=(5,), outputs=(), deliveries=(),
            diagnostics=(), action_plan={5: 2}, output_action={}, delivery_action={},
        )
        assert scope.plan_ids == (2,)
        assert scope.action_ids_of(2) == (5,)
        assert scope.output_ids_of(2, 5) == ()
