"""清理动作执行链接入 run 会话的组件集成测试。

真实 run 会话按轮推进清理流程：到时的清理动作开始执行并固定目标，
成员经真实限制、删除意图与删除/查询独立预算推进到终态；范围来源
可靠确认无产物时动作按成功收场；删除持续失败耗尽预算后按公共错
误终局失败；删除效果未知经查询确认缺席后按缺席事实成功。
"""

from __future__ import annotations

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
from camctl.persistence.initialization import InitOutcome, initialize_state

from ..capture.test_capture_contract import ResultsDouble
from .test_run_dispatch import _parsed
from .test_obtain_flow import (
    _CAMERA_SCHEMA, _await_query, _cancel, _past_schedule, _photo_entry,
    _photo_plan, _scalar)


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
        return frozenset({"camera_take_photo", "delete_action_outputs"})

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


def _config(home: Path) -> object:
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
                        "delete_retry_interval_s": "0",
                        "query_retry_interval_s": "0",
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


def _run_session(deps, cfg, driver: _CleanupDriver, results) -> asyncio.Task:
    registry = _registry(driver)
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
            "cleanup": cleanup_flow(session_cleanup_assembly(
                devices=cfg.devices,
                drivers=registry,
                max_delete_attempts=cfg.cleanup.max_delete_attempts,
                max_query_attempts=cfg.cleanup.max_query_attempts,
            )),
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
