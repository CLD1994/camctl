"""延时完成链的会话级组件集成测试。

真实 run 会话装配（execute_command 与拍摄流程）同契约驱动与结果
列举替身组合：延时任务发送成功后按发送锚点安排等待，到达预计检
查时间先保存等待完成事实，再核实结果集合。完整集合满足时按时间
与产物完成判定保存采集结论并解除占用，动作成功终态与正式产物共
同登记；必需类别缺失保存已知失败，动作失败终态且不补造设备结束
事实，占用保持持有等待残留收场。
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import capture_flow
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.handlers import CaptureRuntime
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.tasks import (
    CaptureTask,
    CompletionMode,
    EndControl,
    StartReturn,
)
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    register_capture_guards,
)
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import (
    TimelapseRepository,
    register_timelapse_guards,
)
from camctl.reporting.policy import register_report_guards, register_sync_guard
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.cancellation import (
    register_cancellation_guards,
)
from camctl.scheduling.rules import LaunchWindow

from ..capture.test_capture_contract import ResultsDouble, _entry
from .test_recording_stop import (
    _await_query,
    _cancel,
    _config_for,
    _future_schedule,
    _scalar,
    _submit_plan,
)
from .test_run_dispatch import _ActivityDriver, _parsed

register_operation_guards()
register_capture_guards()
register_timelapse_guards()
register_report_guards()
register_sync_guard()
register_outputs_guards()
register_cancellation_guards()

pytestmark = pytest.mark.asyncio

_EVIDENCE = EvidenceRegistry(
    (
        EvidenceContract(type="operation_returned", version=1, operation="control",
                         fields=frozenset()),
        EvidenceContract(type="timelapse_sent", version=1, operation="control",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
        EvidenceContract(type="results_returned", version=1, operation="result",
                         fields=frozenset()),
    )
)

_TIMELAPSE_DEFINITION = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"type": {"const": "timelapse"}},
    "required": ["type"],
    "additionalProperties": False,
}

_WAIT_CONFIG = CaptureWaitConfig(target_duration_ms=2_000, driver_margin_ms=0)


class _TimelapseCatalog:
    """受理目录替身：支持 cam-1 的发送后等待延时任务。"""

    def action_types(self):
        return frozenset({"camera_timelapse"})

    def device_exists(self, device_id):
        return device_id == "cam-1"

    def driver_id(self, device_id):
        return "camctl-adb" if device_id == "cam-1" else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type == "camera_timelapse"

    def parameter_definition(self, device_id, action_type, parameter_type):
        if not self.device_supports(device_id, action_type):
            return None
        if parameter_type != "timelapse":
            return None

        def task(params):
            return CaptureTask(
                "camera_timelapse",
                target_duration_s=Decimal("2"),
                duration_based=True,
                wait_after_send=True,
                end_control=EndControl.DEVICE,
                start_return_meaning=StartReturn.SENT,
                completion_mode=CompletionMode.TIME_AND_OUTPUTS,
                stop_supported=False,
                result_wait_margin_s=Decimal("0"),
            )

        return ParameterDefinition(
            schema=_TIMELAPSE_DEFINITION, defaults={}, task_factory=task)


def _timelapse_plan(request_id: str, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [
            {
                "name": "timelapse",
                "type": "camera_timelapse",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at,
                "params": {"type": "timelapse"},
                "policy": {"max_delay_ms": 5000},
            }
        ],
    }


def _timelapse_factory(driver, results, *, check_config: AttemptConfig | None = None):
    def build(owned) -> CaptureRuntime:
        overrides = {} if check_config is None else {"check_config": check_config}
        return CaptureRuntime(
            owned=owned,
            scheduling=SchedulingRepository(),
            operations=OperationRepository(),
            capture=CaptureRepository(),
            timelapse=TimelapseRepository(),
            driver=driver,
            results=results,
            evidence=_EVIDENCE,
            wall_us=lambda: int(time.time() * 1_000_000),
            monotonic_ns=time.monotonic_ns,
            window_of=lambda action: LaunchWindow(
                scheduled_at=action["scheduled_at"],
                window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
            wait_config=lambda params: _WAIT_CONFIG,
            **overrides,
        )

    return build


def _run_session(deps, driver, results, *, check_config: AttemptConfig | None = None):
    return asyncio.create_task(execute_command(
        deps, None,
        flows={"scheduling": capture_flow(
            _timelapse_factory(driver, results, check_config=check_config))},
        poll_interval_s=0.1,
    ))


class TestTimelapseSatisfiedFinish:
    async def test_wait_completes_and_confirms_time_and_outputs(
            self, tmp_path: Path) -> None:
        home = tmp_path / "satisfied"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        catalog = _TimelapseCatalog()
        await _submit_plan(
            tmp_path, cfg, catalog, _timelapse_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        driver = _ActivityDriver()
        results = ResultsDouble({})
        task = _run_session(deps, driver, results)
        try:
            # 发送成功：保存发送事实与等待安排，设备活动保持未知。
            await _await_query(
                db,
                "SELECT dispatch_state, activity_state, occupancy_state,"
                " expected_check_at IS NOT NULL FROM device_activities"
                " WHERE id = 1", (3, 1, 1, 1))
            assert driver.calls == ["start_timelapse"]
            # 完整结果先于预计检查时间出现在设备上。
            results.files_by_action[1] = (_entry("sequence-1"),)
            # 到达预计检查时间：等待完成、集合核实与终态按序保存。
            await _await_query(db, "SELECT status FROM actions WHERE id = 1", (3,))
            row = _scalar(
                db,
                "SELECT result_set_state, completion_basis, occupancy_state,"
                " activity_state, wait_completed_event_id IS NOT NULL,"
                " json_extract(capture_json, '$.status')"
                " FROM device_activities WHERE id = 1")
            assert row == (3, 3, 2, 1, 1, "completed")
            check = _scalar(
                db,
                "SELECT json_extract(result_check_json, '$.outcome'),"
                " json_extract(completion_evidence_json, '$.method'),"
                " json_extract(completion_evidence_json,"
                " '$.wait_completed_event_id') ="
                " wait_completed_event_id FROM device_activities WHERE id = 1")
            assert check == (1, "time_and_outputs", 1)
            assert _scalar(
                db, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
                ) == (1,)
            assert _scalar(
                db, "SELECT status FROM plans WHERE id = 1") == (3,)
        finally:
            await _cancel(task)
        close_runtime(deps)


class TestTimelapseUnmetFinish:
    async def test_missing_required_kind_saves_known_failure(
            self, tmp_path: Path) -> None:
        home = tmp_path / "unmet"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        catalog = _TimelapseCatalog()
        await _submit_plan(
            tmp_path, cfg, catalog, _timelapse_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        driver = _ActivityDriver()
        task = _run_session(deps, driver, ResultsDouble({}))
        try:
            # 必需类别始终缺失：到检查时间保存明确不满足的已知失败。
            await _await_query(db, "SELECT status FROM actions WHERE id = 1", (4,))
            row = _scalar(
                db,
                "SELECT result_set_state, completion_basis, occupancy_state,"
                " activity_state, json_extract(capture_json, '$.status')"
                " FROM device_activities WHERE id = 1")
            assert row == (3, 4, 1, 1, "failed")
            assert _scalar(
                db, "SELECT COUNT(*) FROM outputs") == (0,)
            assert _scalar(
                db, "SELECT status FROM plans WHERE id = 1") == (3,)
        finally:
            await _cancel(task)
        close_runtime(deps)


class TestTimelapseCheckRounds:
    async def test_incomplete_file_retries_next_round_then_succeeds(
            self, tmp_path: Path) -> None:
        home = tmp_path / "retry-round"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        catalog = _TimelapseCatalog()
        await _submit_plan(
            tmp_path, cfg, catalog, _timelapse_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        driver = _ActivityDriver()
        # 第一轮：必需类别已在设备上但写入尚未完成，属于暂不齐备。
        results = ResultsDouble({1: (_entry("sequence-1", complete=False),)})
        task = _run_session(deps, driver, results)
        try:
            await _await_query(
                db,
                "SELECT status, attempts_used, retry_wait_required"
                " FROM operation_runs WHERE responsibility_key = 'results/1'",
                (2, 1, 1))
            assert _scalar(
                db,
                "SELECT a.status FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id = r.id"
                " WHERE r.responsibility_key = 'results/1'"
                " AND a.attempt_no = 1") == (2,)
            assert _scalar(
                db,
                "SELECT result_set_state FROM device_activities"
                " WHERE id = 1") == (1,)
            # 下一轮文件写完：新轮次尝试后结论、流程结束与终态同链保存。
            results.files_by_action[1] = (_entry("sequence-1"),)
            await _await_query(db, "SELECT status FROM actions WHERE id = 1", (3,))
            run = _scalar(
                db,
                "SELECT status, attempts_used, retry_wait_required"
                " FROM operation_runs WHERE responsibility_key = 'results/1'")
            assert run == (3, 2, 0)
            assert _scalar(
                db,
                "SELECT a.status FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id = r.id"
                " WHERE r.responsibility_key = 'results/1'"
                " AND a.attempt_no = 2") == (2,)
            assert _scalar(
                db,
                "SELECT result_set_state, completion_basis, occupancy_state"
                " FROM device_activities WHERE id = 1") == (3, 3, 2)
            assert _scalar(
                db, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
                ) == (1,)
        finally:
            await _cancel(task)
        close_runtime(deps)

    async def test_exhausted_rounds_close_unconfirmed(
            self, tmp_path: Path) -> None:
        home = tmp_path / "exhausted"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        catalog = _TimelapseCatalog()
        await _submit_plan(
            tmp_path, cfg, catalog, _timelapse_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        driver = _ActivityDriver()
        results = ResultsDouble({1: (_entry("sequence-1", complete=False),)})
        task = _run_session(
            deps, driver, results,
            check_config=AttemptConfig(
                max_attempts=1, timeout_s=Decimal("10"),
                retry_interval_s=Decimal("3")))
        try:
            # 单轮预算用尽后不再列举：核实流程与集合结论同事务收场为
            # 无法确认，动作按登记的公共错误失败，占用保持等待残留收场。
            await _await_query(db, "SELECT status FROM actions WHERE id = 1", (4,))
            row = _scalar(
                db,
                "SELECT result_set_state, completion_basis, occupancy_state,"
                " json_extract(capture_json, '$.status')"
                " FROM device_activities WHERE id = 1")
            assert row == (4, 1, 1, "unconfirmed")
            assert _scalar(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'results/1'") == (6, 1)
            failure = _scalar(
                db,
                "SELECT error_code, json_extract(error_details_json, '$.reason')"
                " FROM actions WHERE id = 1")
            assert failure == (12, "outputs_unknown")
        finally:
            await _cancel(task)
        close_runtime(deps)

    async def test_listing_failure_consumes_round_then_recovers(
            self, tmp_path: Path) -> None:
        home = tmp_path / "listing-failure"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        catalog = _TimelapseCatalog()
        await _submit_plan(
            tmp_path, cfg, catalog, _timelapse_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        driver = _ActivityDriver()

        class _FlakyResults:
            """第一轮列举抛错，之后返回完整结果。"""

            def __init__(self) -> None:
                self.failed = False

            async def list_files(self, action_id: int) -> tuple:
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("listing transport failed")
                return (_entry("sequence-1"),)

        flaky = _FlakyResults()
        task = _run_session(deps, driver, flaky)
        try:
            # 失败轮次保存失败尝试并建立重试等待；下一轮新尝试后成功。
            await _await_query(db, "SELECT status FROM actions WHERE id = 1", (3,))
            run = _scalar(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'results/1'")
            assert run == (3, 2)
            attempts = _scalar(
                db,
                "SELECT a.status FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id = r.id"
                " WHERE r.responsibility_key = 'results/1'"
                " AND a.attempt_no = 1")
            assert attempts == (3,)
            assert _scalar(
                db,
                "SELECT result_set_state, completion_basis"
                " FROM device_activities WHERE id = 1") == (3, 3)
        finally:
            await _cancel(task)
        close_runtime(deps)
