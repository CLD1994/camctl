"""D2 能力、绑定与证据的组件集成测试。

真实持久化保存动作的设备绑定；受端口约束的驱动替身经同一证据
登记验证观察。拍摄、跨设备取回、清理及独立收场在配置变化后都
从原绑定读取，绑定错误只影响相应任务。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import (
    AcceptanceContext,
    CommandMode,
    accept_input,
)
from camctl.acceptance.ports import ParameterDefinition
from camctl.devices.tasks import CaptureTask
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import (
    BindingStatus,
    DeviceBinding,
    check_binding,
)
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceError,
    EvidenceRegistry,
    validate_observation,
)
from camctl.devices.ports import (
    ControlRequest,
    DeviceCallResult,
    DriverDeclaration,
    StopDriver,
)
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

register_acceptance_guards()

pytestmark = pytest.mark.asyncio

_NOW = 1_750_000_000_000_000

_RECORD_DEFINITION = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"type": {"const": "timed"}, "duration_s": {"type": "integer"}},
    "required": ["type"],
    "additionalProperties": False,
}

STOP_CONFIRMED = EvidenceContract(
    type="stop_confirmed",
    version=1,
    operation="stop",
    fields=frozenset({"activity_id"}),
    identity_field="activity_id",
    max_observations=1,
)
REGISTRY = EvidenceRegistry([STOP_CONFIRMED])


class Catalog:
    """受静态目录端口约束的两设备替身。"""

    def action_types(self):
        return frozenset({"camera_record", "obtain_action_outputs", "delete_action_outputs"})

    def device_exists(self, device_id):
        return device_id in {"cam-1", "cam-2"}

    def driver_id(self, device_id):
        return {"cam-1": "camctl-adb", "cam-2": "vendor-x"}.get(device_id)

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type.startswith("camera_")

    def parameter_definition(self, device_id, action_type, parameter_type):
        if action_type == "camera_record" and device_id in {"cam-1", "cam-2"}:
            return ParameterDefinition(schema=_RECORD_DEFINITION, defaults={"duration_s":60}, preview_supported=False,
                task_factory=lambda params: CaptureTask("camera_record", target_duration_s=params["duration_s"], stop_supported=True))
        return None


class TerminalStopDouble:
    """受 StopDriver 端口与证据登记约束的停止替身。"""

    declaration = DriverDeclaration(
        control_supported=True,
        stop_supported=True,
        query_supported=False,
        result_supported=False,
        read_supported=False,
        digest_supported=False,
        delete_supported=False,
    )

    def __init__(self, activity_id: str) -> None:
        self._activity_id = activity_id

    async def stop(self, request: ControlRequest) -> DeviceCallResult:
        observation = DeviceObservation(
            type="stop_confirmed", version=1, data={"activity_id": self._activity_id}
        )
        validate_observation(observation, REGISTRY.contract("stop_confirmed", 1))
        return DeviceCallResult(observations=(observation,), error=None)


@pytest.fixture()
def environment(tmp_path: Path):
    _create_valid_database(tmp_path / "state.db")
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    context = AcceptanceContext(
        mode=CommandMode.SUBMIT,
        catalog=Catalog(),
        repository=AcceptanceRepository(),
        clock=type("Clock", (), {"utc_micros": staticmethod(lambda: _NOW)})(),
    )
    yield owned.connection, context
    owned.connection.close()


def _plan_body() -> dict:
    return {
        "request_id": "42",
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": [
            {
                "name": "主录像",
                "type": "camera_record",
                "device_id": "cam-1",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"type": "timed"},
                "policy": {"max_delay_ms": 1000},
            },
            {
                "name": "辅录像",
                "type": "camera_record",
                "device_id": "cam-2",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"type": "timed"},
                "policy": {"max_delay_ms": 1000},
            },
        ],
    }


async def _accept(environment, tmp_path: Path) -> None:
    connection, context = environment
    target = tmp_path / "plan.json"
    target.write_text(
        __import__("json").dumps(_plan_body(), ensure_ascii=False), encoding="utf-8"
    )
    from camctl.persistence.runtime import OwnedConnection

    read = await read_input(str(target), _Reader())
    await accept_input(
        parse_input(read),
        context,
        new_operation_key(),
        OwnedConnection(connection=connection, metadata=None),
    )


class _Reader:
    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()


def _saved_bindings(connection: sqlite3.Connection) -> dict[str, DeviceBinding]:
    rows = connection.execute(
        "SELECT name, device_id, driver_id FROM actions WHERE device_id IS NOT NULL"
    ).fetchall()
    return {
        name: DeviceBinding(device_id=device, driver_id=driver)
        for name, device, driver in rows
    }


def _config(devices: dict) -> object:
    return load_config({"devices": devices}, ConfigDefaults())


async def test_binding_errors_cover_capture_retrieval_cleanup_and_shutdown(
    environment, tmp_path: Path
) -> None:
    connection, _ = environment
    await _accept(environment, tmp_path)
    bindings = _saved_bindings(connection)
    assert set(bindings) == {"主录像", "辅录像"}

    # 拍摄入口：目标设备已从配置移除，原绑定保持。
    capture = check_binding(bindings["主录像"], _config({"cam-2": {"kind": "camera", "driver": "vendor-x"}}))
    assert capture.status is BindingStatus.DEVICE_MISSING
    assert capture.binding.driver_id == "camctl-adb"

    # 跨设备取回与清理：源动作设备改绑其他驱动，原驱动仍可读。
    retrieval = check_binding(
        bindings["辅录像"],
        _config(
            {
                "cam-1": {"kind": "camera", "driver": "camctl-adb"},
                "cam-2": {"kind": "camera", "driver": "rewired"},
            }
        ),
    )
    assert retrieval.status is BindingStatus.DRIVER_MISMATCH
    assert retrieval.current_driver_id == "rewired"
    assert retrieval.binding.driver_id == "vendor-x"

    # 清理目标设备声明不可靠读取：正常 load_config 会在配置层拒绝
    # 该声明，此分区覆盖直接构造快照的读取路径，不折叠为缺失。
    cleanup = check_binding(
        bindings["辅录像"], type("S", (), {"devices": {"cam-2": {"kind": "camera"}}})()
    )
    assert cleanup.status is BindingStatus.UNAVAILABLE

    # 独立收场：停止替身按原绑定执行，观察经同一证据登记验证。
    stop = TerminalStopDouble(activity_id="7")
    result = await stop.stop(
        ControlRequest(
            operation="stop", binding=capture.binding, params={}
        )
    )
    assert result.error is None
    assert len(result.observations) == 1
    with pytest.raises(EvidenceError):
        validate_observation(
            result.observations[0],
            STOP_CONFIRMED,
            expected_identity="9",
        )


async def test_matched_binding_lets_all_operations_proceed(environment, tmp_path: Path) -> None:
    connection, _ = environment
    await _accept(environment, tmp_path)
    bindings = _saved_bindings(connection)
    config = _config(
        {
            "cam-1": {"kind": "camera", "driver": "camctl-adb"},
            "cam-2": {"kind": "camera", "driver": "vendor-x"},
        }
    )
    for binding in bindings.values():
        assert check_binding(binding, config).status is BindingStatus.MATCHED
