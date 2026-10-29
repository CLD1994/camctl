"""默认拍摄流程经真实工厂消费原 CLOSED 输入，不访问当前设备。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.bootstrap.flows import capture_flow
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import (
    ControlDriver, DeleteDriver, DigestDriver, DriverDeclaration, ReadDriver,
    ResultDriver, StateQueryDriver, StopDriver,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..capture.result_consumer_fixtures import RESULT_EVIDENCE
from ..capture.test_closed_result_local_consumption import (
    _assert_local_finish, _assert_preserved, closed_consumer_world,
)
from .test_media_assembly import _config
from ..support.capture import capture_database


pytestmark = pytest.mark.asyncio


def _unavailable_device_ports():
    """两个已登记驱动具备真实接口；任何新设备调用都证伪本地资格。"""
    ports = tuple(create_autospec(protocol, instance=True) for protocol in (
        ControlDriver, StopDriver, StateQueryDriver, ResultDriver,
        ReadDriver, DigestDriver, DeleteDriver))
    methods = (ports[0].control, ports[1].stop, ports[2].query_state, ports[3].list_results,
               ports[4].open_read, ports[5].digest, ports[6].delete)
    for method in methods:
        method.side_effect = AssertionError("原 CLOSED 输入本地收尾不得发起新设备调用")
    declaration = DriverDeclaration(True, True, True, True, True, True, False)
    driver = SimpleNamespace(declaration=declaration, control=methods[0], stop=methods[1],
        query_state=methods[2], list_results=methods[3], open_read=methods[4],
        digest=methods[5], delete=methods[6])
    registry = DriverRegistry(tuple(DriverEntry(driver_id, driver, declaration, RESULT_EVIDENCE,
        DriverStatus.SOFTWARE_CONTRACT_VERIFIED) for driver_id in ("camctl-adb", "alternate-camera")))
    return registry, methods


@pytest.mark.parametrize("consumer", ["photo", "timelapse", "canceled_timelapse"])
@pytest.mark.parametrize("binding", ["matched", "missing", "mismatch"])
async def test_default_capture_consumes_closed_unconfirmed_after_binding_change(
        tmp_path, monkeypatch, consumer, binding):
    world = await closed_consumer_world(
        tmp_path, monkeypatch, consumer=consumer, history="latest_complete")
    config = _config(tmp_path)
    assert Path(config.paths.state_db) == world.path
    devices = {} if binding == "missing" else {"cam-1": {
        **config.devices["cam-1"],
        "driver": "alternate-camera" if binding == "mismatch" else "camctl-adb"}}
    registry, methods = _unavailable_device_ports()
    now = world.formed_at + 5_000_000
    # 保持普通媒体装配；不通过手工 runtime 或关闭媒体来替代默认工厂。
    factory = session_capture_assembly(
        devices=devices, drivers=registry, staging=Path(config.paths.staging),
        wait_config=execution_wait_config, wall_us=lambda: now,
        monotonic_ns=lambda: 99_000_000_000)
    database = await capture_database(world.path)
    context = SimpleNamespace(database=database, clock=SimpleNamespace(utc_micros=lambda: now))
    flow = capture_flow(factory)
    before = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert before.metadata == world.metadata
        _assert_preserved(before, world, methods)
        assert before.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (
                2, int(consumer == "canceled_timelapse"))
        assert before.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
    finally:
        before.connection.close()

    await flow(context)
    await flow.settle()
    saved = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        _assert_local_finish(saved, world, methods)
        after = tuple(saved.connection.iterdump())
    finally:
        saved.connection.close()

    await flow(context)
    await flow.settle()
    repeated = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        _assert_local_finish(repeated, world, methods)
        assert tuple(repeated.connection.iterdump()) == after
    finally:
        repeated.connection.close()
