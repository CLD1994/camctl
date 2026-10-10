"""清理动作执行链接入 run 会话的组件集成测试。

真实 run 会话按轮推进清理流程：到时的清理动作开始执行并固定目标，
成员经真实限制、删除意图与删除/查询独立预算推进到终态；范围来源
可靠确认无产物时动作按成功收场；删除持续失败耗尽预算后按公共错
误终局失败；删除效果未知经查询确认缺席后按缺席事实成功。
"""

from __future__ import annotations

from camctl.capture.result_inputs import RESULT_FILES_CONTRACT, RESULT_PAGE_CONTRACT

import asyncio
import contextlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.cleanup_assembly import (
    cleanup_flow, session_cleanup_assembly)
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import cancel_flow, capture_flow
from camctl.bootstrap.lifecycle import (
    build_runtime, close_runtime, execute_command)
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.drivers.registry import (
    DriverEntry, DriverRegistry, DriverStatus)
from camctl.devices.evidence import (
    DeviceObservation, EvidenceContract, EvidenceRegistry)
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.outputs.cleanup_flow import (
    CleanupRuntime, HostArtifactPort, LocalArtifactRequest)
from camctl.persistence.initialization import InitOutcome, initialize_state

from ..capture.test_capture_contract import ResultsDouble
from .test_run_dispatch import _parsed
from .test_obtain_flow import (
    _CAMERA_SCHEMA, _await_query, _cancel, _future_schedule, _past_schedule,
    _photo_entry, _photo_plan, _scalar)


async def _submit(tmp_path: Path, cfg, body: dict) -> None:
    """以清理测试的受理目录提交计划。"""
    deps = build_runtime(CommandMode.SUBMIT, cfg, catalog=_Catalog())
    try:
        outcome = await execute_command(
            deps, await _parsed(tmp_path, body))
        assert outcome.succeeded is True, outcome.details
    finally:
        close_runtime(deps)

pytestmark = pytest.mark.asyncio

_EVIDENCE = EvidenceRegistry(
    (
        EvidenceContract(type="operation_returned", version=1,
                         operation="control", fields=frozenset()),
        EvidenceContract(type="photo_taken", version=1, operation="control",
                         fields=frozenset({"activity_id"}),
                         identity_field="activity_id"),
        EvidenceContract(type="results_returned", version=1,
                         operation="result", fields=frozenset()),
        RESULT_FILES_CONTRACT,
        RESULT_PAGE_CONTRACT,
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


class _Catalog:
    """受理目录替身：支持 cam-1 的照片与清理动作。"""

    def action_types(self):
        return frozenset(
            {"camera_take_photo", "delete_action_outputs", "cancel_task"})

    def device_exists(self, device_id):
        return device_id == "cam-1"

    def driver_id(self, device_id):
        return "camctl-adb" if device_id == "cam-1" else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type == "camera_take_photo"

    def parameter_definition(self, device_id, action_type, parameter_type):
        from camctl.acceptance.ports import ParameterDefinition

        if (action_type == "camera_take_photo"
                and parameter_type == "single_shot"):
            return ParameterDefinition(schema=_CAMERA_SCHEMA, defaults={})
        return None


class _CleanupDriver:
    """契约替身：拍摄控制与设备删除、存在性查询。

    photo_fails 使拍摄控制持续失败（动作失败无产物）。delete/
    query 按调用序返回配置序列（absent / error / unknown / present），
    序列耗尽沿用最后一个元素；删除与查询观察从请求携带的成员身
    份构造。
    """

    def __init__(self, *, photo_fails: bool = False,
                 delete_results: tuple = (("absent", None),),
                 query_results: tuple = ((None, None),)) -> None:
        self.photo_fails = photo_fails
        self._delete_results = list(delete_results)
        self._query_results = list(query_results)
        self.delete_calls: list[dict] = []
        self.query_calls: list[dict] = []
        self.control_calls: list[str] = []

    def _next(self, results: list) -> tuple:
        if len(results) > 1:
            return results.pop(0)
        return results[0]

    async def control(self, request) -> DeviceCallResult:
        self.control_calls.append(request.operation)
        if self.photo_fails:
            return DeviceCallResult(
                observations=(),
                error={"code": "device_error", "stage": "control"})
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="photo_taken", version=1,
                    data={"activity_id": "1"}),
            ),
            error=None,
        )

    async def delete(self, request) -> DeviceCallResult:
        self.delete_calls.append(dict(request.params))
        kind, error = self._next(self._delete_results)
        observations = ()
        if kind == "absent":
            observations = (
                DeviceObservation(
                    type="file_absent", version=1,
                    data={"cleanup_item_id":
                          request.params["cleanup_item_id"]}),
            )
        return DeviceCallResult(
            observations=observations,
            error=(None if error is None
                   else {"code": "device_error", "stage": "delete"}))

    async def query_state(self, request) -> DeviceCallResult:
        self.query_calls.append(dict(request.params))
        kind, error = self._next(self._query_results)
        observations = ()
        if kind == "present":
            observations = (
                DeviceObservation(
                    type="file_presence", version=1,
                    data={"cleanup_item_id":
                          request.params["cleanup_item_id"],
                          "present": True}),
            )
        elif kind == "absent":
            observations = (
                DeviceObservation(
                    type="file_presence", version=1,
                    data={"cleanup_item_id":
                          request.params["cleanup_item_id"],
                          "present": False}),
            )
        return DeviceCallResult(
            observations=observations,
            error=(None if error is None
                   else {"code": "device_error", "stage": "query"}))


def _config(home: Path, *, delete_retry_interval_s: str = "0",
            query_retry_interval_s: str = "0") -> object:
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            },
            "devices": {
                "cam-1": {
                    "kind": "camera",
                    "driver": "camctl-adb",
                    "cleanup": {
                        "delete_retry_interval_s": delete_retry_interval_s,
                        "query_retry_interval_s": query_retry_interval_s,
                    },
                }
            },
        },
        ConfigDefaults(),
    )


def _registry(driver: _CleanupDriver) -> DriverRegistry:
    return DriverRegistry((
        DriverEntry(
            driver_id="camctl-adb",
            driver=driver,
            declaration=DriverDeclaration(
                control_supported=True,
                stop_supported=False,
                query_supported=True,
                result_supported=False,
                read_supported=False,
                digest_supported=False,
                delete_supported=True,
            ),
            evidence=_EVIDENCE,
            status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
        ),
    ))


def _cleanup_plan(request_id: str, params: dict) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"cleanup-plan-{request_id}",
        "actions": [
            {
                "name": "purge",
                "type": "delete_action_outputs",
                "scheduled_at": _past_schedule(),
                "params": params,
            }
        ],
    }


def _run_session(deps, cfg, driver: _CleanupDriver, results,
                 local_files=None) -> asyncio.Task:
    registry = _registry(driver)
    if local_files is None:
        cleanup = cleanup_flow(session_cleanup_assembly(
            devices=cfg.devices,
            drivers=registry,
            max_delete_attempts=cfg.cleanup.max_delete_attempts,
            max_query_attempts=cfg.cleanup.max_query_attempts,
            staging=Path(cfg.paths.staging),
        ))
    else:
        # 测试注入本地删除替身；设备绑定恒为空，仅本地链路参与。
        from camctl.operations.attempts import AttemptConfig, RetryWaitGate
        from camctl.persistence.repositories.operations import (
            OperationRepository)
        from camctl.persistence.repositories.outputs import OutputsRepository

        retry_gate = RetryWaitGate()

        def factory(owned):
            return CleanupRuntime(
                owned=owned,
                outputs=OutputsRepository(),
                operations=OperationRepository(),
                driver=driver,
                evidence=_EVIDENCE,
                binding_of=lambda item_id: None,
                occurred_at=lambda: int(time.time() * 1_000_000),
                local_files=local_files,
                delete_config=AttemptConfig(
                    max_attempts=cfg.cleanup.max_delete_attempts,
                    timeout_s=None, retry_interval_s=0),
                query_config=AttemptConfig(
                    max_attempts=cfg.cleanup.max_query_attempts,
                    timeout_s=None, retry_interval_s=0),
                retry_gate=retry_gate)

        cleanup = cleanup_flow(factory)
    return asyncio.create_task(execute_command(
        deps, None,
        flows={
            "scheduling": capture_flow(session_capture_assembly(
                devices=cfg.devices,
                drivers=registry,
                results=results,
                staging=Path(cfg.paths.staging),
                wait_config=lambda params: CaptureWaitConfig(
                    target_duration_ms=1_000, driver_margin_ms=0),
            )),
            "cleanup": cleanup,
            "cancel": cancel_flow(ready=Path(cfg.paths.ready),
                                  processing=Path(cfg.paths.processing)),
        },
        poll_interval_s=0.1,
    ))


async def _run_photo_session(tmp_path: Path, cfg, catalog=_Catalog):
    """执行照片动作到成功并返回产物 ID；产物经列举核实登记。"""
    db = Path(cfg.paths.state_db)
    driver = _CleanupDriver()
    results = ResultsDouble({1: (_photo_entry("shot-1", "IMG_0001.jpg"),)})
    deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog())
    task = _run_session(deps, cfg, driver, results)
    try:
        await _await_query(
            db, "SELECT status FROM actions WHERE name='shoot'", (3,))
    finally:
        await _cancel(task)
    close_runtime(deps)
    output = _scalar(db, "SELECT id FROM outputs")
    assert output is not None
    return output[0]


class TestCleanupExecutionLink:
    async def test_cleanup_deletes_source_and_succeeds(
            self, tmp_path: Path) -> None:
        """照片产物被精确清理：一次删除成功，动作与计划成功收场。"""
        cfg = _config(tmp_path)
        db = Path(cfg.paths.state_db)
        assert initialize_state(
            cfg, db).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, _photo_plan("1"))
        output_id = await _run_photo_session(tmp_path, cfg)
        await _submit(tmp_path, cfg, _cleanup_plan(
            "2", {"output_ids": [str(output_id)]}))
        driver = _CleanupDriver()
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(deps, cfg, driver, ResultsDouble({}))
        try:
            await _await_query(
                db, "SELECT status FROM actions WHERE name='purge'", (3,))
            item = _scalar(
                db, "SELECT status, outcome FROM cleanup_items")
            assert item == (4, 1), item
            source = _scalar(
                db, "SELECT availability, cleanup_status FROM outputs")
            assert source == (3, 4), source
            plan = _scalar(
                db, "SELECT status FROM plans WHERE name='cleanup-plan-2'")
            assert plan == (3,), plan
            assert len(driver.delete_calls) == 1, driver.delete_calls
            request = driver.delete_calls[0]
            member = _scalar(db, "SELECT id FROM cleanup_items")
            # 删除请求携带成员与目标文件身份，驱动据此定位文件。
            assert request["cleanup_item_id"] == str(member[0]), request
            assert request["identity_key"], request
        finally:
            await _cancel(task)
        close_runtime(deps)

    async def test_cleanup_scope_without_outputs_succeeds(
            self, tmp_path: Path) -> None:
        """范围来源终态且无产物：固定空集合并按成功收场。"""
        cfg = _config(tmp_path)
        db = Path(cfg.paths.state_db)
        assert initialize_state(
            cfg, db).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, {
            "request_id": "1",
            "created_at": "2026-01-15 08:00:00",
            "name": "photo-cleanup-plan",
            "actions": [
                {
                    "name": "shoot",
                    "type": "camera_take_photo",
                    "device_id": "cam-1",
                    "scheduled_at": _past_schedule(),
                    "params": {"type": "single_shot"},
                    "policy": {"max_delay_ms": 5000},
                },
                {
                    "name": "purge",
                    "type": "delete_action_outputs",
                    "scheduled_at": _past_schedule(),
                    "params": {"source": {"action_name": "shoot"}},
                },
            ],
        })
        driver = _CleanupDriver(photo_fails=True)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(deps, cfg, driver, ResultsDouble({}))
        try:
            await _await_query(
                db, "SELECT status FROM actions WHERE name='shoot'", (4,))
            await _await_query(
                db, "SELECT status FROM actions WHERE name='purge'", (3,))
            row = _scalar(
                db,
                "SELECT target_selection_state FROM actions"
                " WHERE name='purge'")
            assert row == (2,), row
            assert _scalar(db, "SELECT COUNT(*) FROM cleanup_items") == (0,)
            plan = _scalar(
                db, "SELECT status FROM plans WHERE name='photo-cleanup-plan'")
            assert plan == (3,), plan
            assert driver.delete_calls == []
        finally:
            await _cancel(task)
        close_runtime(deps)

    async def test_cleanup_delete_exhaustion_fails_action(
            self, tmp_path: Path) -> None:
        """删除持续错误且查询确认在场：删除预算耗尽后终局失败。"""
        cfg = _config(tmp_path)
        db = Path(cfg.paths.state_db)
        assert initialize_state(
            cfg, db).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, _photo_plan("1"))
        output_id = await _run_photo_session(tmp_path, cfg)
        await _submit(tmp_path, cfg, _cleanup_plan(
            "2", {"output_ids": [str(output_id)]}))
        driver = _CleanupDriver(
            delete_results=(("error", None),),
            query_results=(("present", True),))
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(deps, cfg, driver, ResultsDouble({}))
        try:
            await _await_query(
                db, "SELECT status FROM actions WHERE name='purge'", (4,))
            item = _scalar(
                db, "SELECT status, error_code FROM cleanup_items")
            # 成员错误码是明细表整数编号（delete_attempts_exhausted=2）。
            assert item == (5, 2), item
            details = _scalar(
                db,
                "SELECT json_extract(error_details_json, '$.attempts_used')"
                " FROM cleanup_items")
            assert details == (3,), details
            action = _scalar(
                db, "SELECT error_code FROM actions WHERE name='purge'")
            assert action is not None and action[0] != "delete_attempts_exhausted"
            assert len(driver.delete_calls) == 3, driver.delete_calls
            assert len(driver.query_calls) == 3, driver.query_calls
        finally:
            await _cancel(task)
        close_runtime(deps)

    async def test_cleanup_unknown_delete_confirmed_absent(
            self, tmp_path: Path) -> None:
        """删除效果未知经查询确认缺席：按存在性查询依据成功。"""
        cfg = _config(tmp_path)
        db = Path(cfg.paths.state_db)
        assert initialize_state(
            cfg, db).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, _photo_plan("1"))
        output_id = await _run_photo_session(tmp_path, cfg)
        await _submit(tmp_path, cfg, _cleanup_plan(
            "2", {"output_ids": [str(output_id)]}))
        driver = _CleanupDriver(
            delete_results=(("unknown", None),),
            query_results=(("absent", None),))
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(deps, cfg, driver, ResultsDouble({}))
        try:
            await _await_query(
                db, "SELECT status FROM actions WHERE name='purge'", (3,))
            item = _scalar(
                db, "SELECT status, outcome FROM cleanup_items")
            assert item == (4, 3), item
            presence = _scalar(
                db, "SELECT presence_state FROM device_files")
            assert presence == (3,), presence
            assert len(driver.delete_calls) == 1
            assert len(driver.query_calls) == 1
        finally:
            await _cancel(task)
        close_runtime(deps)


def _cancel_plan(request_id: str, target_id: int, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [
            {
                "name": "cancel",
                "type": "cancel_task",
                "scheduled_at": scheduled_at,
                "params": {"target": {"action_instance_id": str(target_id)}},
            }
        ],
    }


class TestCleanupCancelSettlement:
    async def test_cancel_with_deleting_member_settles_and_cancels_action(
            self, tmp_path: Path) -> None:
        """取消生效时删除中成员经执行链收场，动作以取消终态结束。

        删除调用失败且核实查询仍未知，成员在双等待窗口内跨轮保持
        删除中；取消请求在窗口内生效后，执行链接手该成员并用查询
        预算确认缺席，成员按缺席事实成功收场；取消结算把动作终态
        化为取消，取消动作成功且完成依据为取消达成，两个计划都进
        入完成。
        """
        # 删除与查询重试间隔拉长：首次失败与未知核实后成员在等待
        # 窗口内保持删除中，取消计划在窗口内到期生效。
        cfg = _config(
            tmp_path, delete_retry_interval_s="30",
            query_retry_interval_s="30")
        db = Path(cfg.paths.state_db)
        assert initialize_state(
            cfg, db).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, _photo_plan("1"))
        output_id = await _run_photo_session(tmp_path, cfg)
        await _submit(tmp_path, cfg, _cleanup_plan(
            "2", {"output_ids": [str(output_id)]}))
        purge_id = _scalar(db, "SELECT id FROM actions WHERE name='purge'")[0]
        await _submit(tmp_path, cfg, _cancel_plan(
            "3", int(purge_id), _future_schedule(3)))
        driver = _CleanupDriver(
            delete_results=(("error", None),),
            query_results=(("unknown", None), ("absent", None)))
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(deps, cfg, driver, ResultsDouble({}))
        try:
            # 首次删除失败后成员保持删除中，等待窗口内取消生效。
            await _await_query(db, "SELECT status FROM cleanup_items", (3,))
            await _await_query(
                db, "SELECT cancel_requested FROM actions WHERE name='purge'",
                (1,))
            # 执行链接手取消分支：查询确认缺席后成员成功收场。
            await _await_query(
                db, "SELECT status, outcome FROM cleanup_items", (4, 3))
            # 取消结算把目标动作终态化为取消；取消动作成功。
            await _await_query(
                db, "SELECT status FROM actions WHERE name='purge'", (6,))
            await _await_query(
                db, "SELECT status FROM actions WHERE name='cancel'", (3,))
            item = _scalar(db, "SELECT status, outcome FROM cancel_items")
            assert item == (3, 1), item
            cleanup_plan = _scalar(
                db, "SELECT status FROM plans WHERE name='cleanup-plan-2'")
            assert cleanup_plan == (3,), cleanup_plan
            cancel_plan = _scalar(
                db, "SELECT status FROM plans WHERE name='plan-3'")
            assert cancel_plan == (3,), cancel_plan
            # 取消生效后不再重试删除；窗口内一次未知核实加收场一
            # 次缺席核实，共两次查询。
            assert len(driver.delete_calls) == 1, driver.delete_calls
            assert len(driver.query_calls) == 2, driver.query_calls
        finally:
            await _cancel(task)
        close_runtime(deps)


def _seed_repaired_artifact(db: Path, cfg, output_id: int) -> None:
    """把照片产物改为主机修复成品承载；staging 建对应真实文件。"""
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO intermediate_files (id, owner_action_id, purpose,"
            " relative_path, retention_state, cleanup_state, size_bytes,"
            " created_event_id, last_event_id, change_count)"
            " VALUES (900, (SELECT source_action_id FROM outputs"
            " WHERE id = ?), 4, 'derived/900.jpg', 3, 1, 16, 1, 1, 1)",
            (output_id,))
        connection.execute(
            "UPDATE outputs SET kind = 2, device_file_id = NULL,"
            " intermediate_file_id = 900 WHERE id = ?", (output_id,))
        connection.commit()
    finally:
        connection.close()
    derived = Path(cfg.paths.staging) / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    (derived / "900.jpg").write_bytes(b"repaired-artifact")


class TestHostArtifactCleanup:
    async def test_repaired_artifact_cleanup_deletes_local_file(
            self, tmp_path: Path) -> None:
        """主机修复成品成员删除 staging 对应成品并按删除事实收场。

        修复成品由提升的中间文件承载，不属于设备删除目标：成员经
        本地文件删除推进，成品文件从 staging/derived 消失，中间文
        件保存清理完成，产物进入已清理投影，动作与计划成功收场；
        设备驱动不收到任何删除或查询调用。
        """
        cfg = _config(tmp_path)
        db = Path(cfg.paths.state_db)
        assert initialize_state(
            cfg, db).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, _photo_plan("1"))
        output_id = await _run_photo_session(tmp_path, cfg)
        _seed_repaired_artifact(db, cfg, output_id)
        await _submit(tmp_path, cfg, _cleanup_plan(
            "2", {"output_ids": [str(output_id)]}))
        driver = _CleanupDriver()
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(deps, cfg, driver, ResultsDouble({}))
        try:
            await _await_query(
                db, "SELECT status FROM actions WHERE name='purge'", (3,))
            item = _scalar(db, "SELECT status, outcome FROM cleanup_items")
            assert item == (4, 1), item
            local = _scalar(
                db, "SELECT cleanup_state FROM intermediate_files")
            assert local == (4,), local
            output = _scalar(
                db, "SELECT availability, cleanup_status FROM outputs")
            assert output == (3, 4), output
            artifact = Path(cfg.paths.staging) / "derived" / "900.jpg"
            assert not artifact.exists()
            assert driver.delete_calls == []
            assert driver.query_calls == []
            plan = _scalar(
                db, "SELECT status FROM plans WHERE name='cleanup-plan-2'")
            assert plan == (3,), plan
        finally:
            await _cancel(task)
        close_runtime(deps)

    async def test_repaired_artifact_unknown_delete_confirmed_absent(
            self, tmp_path: Path) -> None:
        """本地删除结果未知经查询确认缺席：按存在性依据成功收场。

        删除调用失败不解释文件仍在，核实查询确认缺席后成员按缺席
        依据成功，中间文件保存清理完成，动作与计划成功；本地删除
        只调用一次，设备驱动不参与。
        """
        cfg = _config(tmp_path)
        db = Path(cfg.paths.state_db)
        assert initialize_state(
            cfg, db).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, _photo_plan("1"))
        output_id = await _run_photo_session(tmp_path, cfg)
        _seed_repaired_artifact(db, cfg, output_id)
        await _submit(tmp_path, cfg, _cleanup_plan(
            "2", {"output_ids": [str(output_id)]}))
        # 本地删除失败（结果未知），核实查询确认缺席。
        local = _LocalArtifacts(
            delete_results=(("error", None),),
            query_results=(("absent", None),))
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(deps, cfg, _CleanupDriver(), ResultsDouble({}),
                            local_files=local)
        try:
            await _await_query(
                db, "SELECT status FROM actions WHERE name='purge'", (3,))
            item = _scalar(db, "SELECT status, outcome FROM cleanup_items")
            assert item == (4, 3), item
            local_state = _scalar(
                db, "SELECT cleanup_state FROM intermediate_files")
            assert local_state == (4,), local_state
            output = _scalar(
                db, "SELECT availability, cleanup_status FROM outputs")
            assert output == (3, 4), output
            assert len(local.delete_calls) == 1, local.delete_calls
            assert len(local.query_calls) == 1, local.query_calls
        finally:
            await _cancel(task)
        close_runtime(deps)


class _LocalArtifacts(HostArtifactPort):
    """本地删除替身：按调用序返回配置结果，观察与真实适配器同形。

    delete 的 absent 表示可靠缺席，error 表示调用失败（结果未
    知）；query 的 absent/present 表示可靠在场事实。
    """

    def __init__(self, *, delete_results: tuple, query_results: tuple) -> None:
        self._delete_results = list(delete_results)
        self._query_results = list(query_results)
        self.delete_calls: list[LocalArtifactRequest] = []
        self.query_calls: list[LocalArtifactRequest] = []

    def _next(self, results: list) -> tuple:
        if len(results) > 1:
            return results.pop(0)
        return results[0]

    async def delete(self, request: LocalArtifactRequest) -> DeviceCallResult:
        self.delete_calls.append(request)
        kind, error = self._next(self._delete_results)
        observations = ()
        if kind == "absent":
            observations = (DeviceObservation(
                type="file_absent", version=1,
                data={"cleanup_item_id": str(request.cleanup_item_id)}),)
        return DeviceCallResult(
            observations=observations,
            error=(None if error is None
                   else {"code": "local_io_failed", "stage": "delete"}))

    async def query_state(self, request: LocalArtifactRequest) -> DeviceCallResult:
        self.query_calls.append(request)
        kind, error = self._next(self._query_results)
        observations = ()
        if kind in ("present", "absent"):
            observations = (DeviceObservation(
                type="file_presence", version=1,
                data={"cleanup_item_id": str(request.cleanup_item_id),
                      "present": kind == "present"}),)
        return DeviceCallResult(
            observations=observations,
            error=(None if error is None
                   else {"code": "local_io_failed", "stage": "query"}))
