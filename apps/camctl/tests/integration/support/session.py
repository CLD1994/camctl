"""通过正式会话与拍摄装配准备真实协作者，不重建业务推进逻辑。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
from pathlib import Path
import sqlite3
import time

from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.acceptance.input import FileInputReader, parse_input, read_input
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.devices.definitions_runtime import reset_driver_definitions
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.drivers.runtime import register_drivers, reset_drivers
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.persistence.repositories.transactions import encode_json_value


def session_config(home, *, devices=None, **sections):
    """每个世界使用独立目录，其他配置继续由生产加载器校验。"""
    return load_config({
        "paths": {name: str(Path(home) / filename) for name, filename in (
            ("state_db", "state.db"), ("log_file", "camctl.log"),
            ("staging", "staging"), ("ready", "ready"), ("processing", "processing"))},
        **({} if devices is None else {"devices": devices}), **sections,
    }, ConfigDefaults())


async def parsed_plan(directory, body):
    """真实文件读取与正式输入解析，保留读取诊断和原请求。"""
    target = Path(directory) / "plan.json"
    target.write_text(encode_json_value(body), encoding="utf-8")
    return parse_input(await read_input(str(target), FileInputReader()))



async def submit_plan(directory, config, catalog, body):
    deps = build_runtime(CommandMode.SUBMIT, config, catalog=catalog)
    try:
        outcome = await execute_command(deps, await parsed_plan(directory, body))
        assert outcome.succeeded, outcome.details
        return outcome
    finally:
        close_runtime(deps)


@asynccontextmanager
async def running_session(config, catalog, source=None, *, entry=None, **options):
    """拥有完整默认会话及实际收场；用例只提交输入、端口与公开观察。"""
    deps = build_runtime(CommandMode.RUN, config, catalog=catalog)
    operation = (execute_command(deps, source, **options) if entry is None
                 else run_default_session(deps, source, entry, **options))
    task = asyncio.create_task(operation)
    try:
        yield deps, task
    finally:
        try:
            await cancel_session(task)
        finally:
            close_runtime(deps)


class ResultDriver:
    """将受结果页契约约束的替身接到驱动的正式 result 端口。"""

    def __init__(self, results):
        self.results = results

    async def list_results(self, request, batch):
        # 单页替身明确拒绝分页输入；分页专项提供自己的目录驱动。
        assert request.params.get("cursor") is None
        page = await self.results.list_page(
            request.ticket, cursor=None, timeout_s=request.timeout_s,
            output_scope=request.params.get("output_scope"),
            completion_context=request.params.get("completion_context"),
            video_result_mode=request.params.get("video_result_mode"))
        return DeviceCallResult.from_outcome(page.outcome)


class CapturePorts:
    """按正式驱动端口组合控制、停止与结果协作者。"""

    def __init__(self, control, *, stop=None, results=None):
        self.control = control.control
        if stop is not None:
            self.stop = stop.stop
        if results is not None:
            self.list_results = results.list_results


def capture_entry(driver, evidence, *, driver_id="camctl-adb", stopper=None,
                  results_driver=None):
    return DriverEntry(
        driver_id=driver_id,
        driver=CapturePorts(driver, stop=stopper, results=results_driver),
        declaration=DriverDeclaration(
            control_supported=True, stop_supported=stopper is not None,
            query_supported=False, result_supported=results_driver is not None,
            read_supported=False, digest_supported=False, delete_supported=False),
        evidence=evidence,
        status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
    )


def capture_factory(config, driver, results, evidence, *, stopper=None,
                    monotonic_ns=None, wall_us=None, wait_config=None,
                    check_config=None, extra_wait_ms=None, driver_id="camctl-adb"):
    """从正式装配得到异步工厂，运行时由生产装配创建并共享会话状态。"""
    entry = capture_entry(driver, evidence, driver_id=driver_id, stopper=stopper,
                          results_driver=None if results is None else ResultDriver(results))
    devices = {name: dict(value) for name, value in config.devices.items()}
    if not devices:
        devices["cam-1"] = {"kind": "camera", "driver": driver_id}
    if extra_wait_ms is not None:
        for declaration in devices.values():
            declaration["capture"] = {"extra_wait_ms": extra_wait_ms}
    if check_config is not None:
        for declaration in devices.values():
            declaration["result_check"] = {
                "max_attempts": check_config.max_attempts,
                "timeout_s": check_config.timeout_s,
                "retry_interval_s": check_config.retry_interval_s,
            }
    return session_capture_assembly(
        devices=devices, drivers=DriverRegistry((entry,)), results=results,
        staging=Path(config.paths.staging),
        wait_config=execution_wait_config if wait_config is None else wait_config,
        monotonic_ns=monotonic_ns, wall_us=wall_us,
    )


async def run_default_session(deps, source, entry, **options):
    """登记端口后运行完整生产会话；execute_command 使用默认流程。"""
    reset_drivers()
    reset_driver_definitions()
    register_drivers(entry)
    try:
        return await execute_command(deps, source, **options)
    finally:
        reset_drivers()
        reset_driver_definitions()


async def cancel_session(task):
    """等待会话实际收场，同时保留已发生的执行异常。"""
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def query_database(path, sql, params=()):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql, params).fetchone()


async def await_database(path, sql, expected, *, timeout_s=15.0, task=None):
    """保留成功预算；会话明确结束后立即交付结果和数据库现状。"""
    deadline = time.monotonic() + timeout_s
    while True:
        actual = query_database(path, sql)
        if actual == expected:
            return
        if task is not None and task.done():
            outcome = await task
            raise AssertionError(
                f"会话已结束，等待 {sql!r} = {expected!r}，实际 {actual!r}；"
                f"会话结果 {outcome!r}")
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"等待 {sql!r} = {expected!r} 超时，实际 {actual!r}")
        await asyncio.sleep(0.05)


async def accept_plan(database, directory, catalog, body, clock, *, mode=CommandMode.RUN):
    """受控时间场景仍经实际文件解析、公开受理与真实业务数据库保存。"""
    from camctl.acceptance.service import AcceptanceContext, accept_input
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.acceptance import AcceptanceRepository, register_acceptance_guards

    register_acceptance_guards()
    result = await accept_input(await parsed_plan(directory, body), AcceptanceContext(
        mode=mode, catalog=catalog, repository=AcceptanceRepository(), clock=clock),
        new_operation_key(), database)
    assert result.plan_id is not None, result
    return result


async def open_session_database(path, *, factory=None):
    """连接工厂只在数据库线程执行；会话拥有并关闭返回的公开端口。"""
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

    if factory is None:
        factory = lambda: open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
    return await BusinessDatabase.open(factory)


def session_context(path, *, clock, catalog=None, facts_query=None,
                    clock_policy=None, **overrides):
    """显式流程专项共用的正式会话装配，真实锁和数据库由 run_session 收场。"""
    from camctl.bootstrap.application import query_work_facts
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.session import SessionRepository
    from camctl.session.clock import ClockCheckInput, register_clock_guard
    from camctl.session.locks import acquire_admission, acquire_session, lock_file_paths
    from camctl.session.service import SessionContext, SessionPaths

    register_clock_guard()
    path = Path(path)
    session_lock, admission_lock = lock_file_paths(path)
    values = dict(
        mode=CommandMode.RUN, catalog=catalog, clock=clock,
        open_database=lambda: open_session_database(path),
        acceptance_repository=AcceptanceRepository(), session_repository=SessionRepository(),
        paths=SessionPaths(session_lock=session_lock, admission_lock=admission_lock),
        clock_policy=(lambda connection: ClockCheckInput(None, 0, 0, 1))
            if clock_policy is None else clock_policy,
        acquire_session=lambda: acquire_session(session_lock),
        acquire_admission=lambda: acquire_admission(admission_lock),
        facts_query=query_work_facts if facts_query is None else facts_query,
    )
    values.update(overrides)
    return SessionContext(**values)
