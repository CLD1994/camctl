"""D5 驱动契约与实际接入范围的组件集成测试。

受真实端口约束的替身驱动与业务消费者替身（C6 结果核实、X4 拷贝
续传、X8 源清理）协作：声明的证据契约与消费者接受的事实精确一
致；不支持的能力不可被消费者调用；能力缺失与调用失败分开。软件
契约验证与实际设备验收分别有状态与可复查输入，实际相机联调另
行安排，不在软件门禁内。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.registry import (
    CapabilityNotDeclaredError,
    DriverEntry,
    DriverRegistry,
    DriverStatus,
    UnknownOperationError,
    port_for,
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
)

pytestmark = pytest.mark.asyncio

#: 每组消费者受其模块计划约束的证据契约。
RESULT_LISTED = EvidenceContract(
    type="result_files_listed",
    version=1,
    operation="result",
    fields=frozenset({"activity_id", "entries"}),
    identity_field="activity_id",
)
DIGEST_COMPUTED = EvidenceContract(
    type="file_digest",
    version=1,
    operation="digest",
    fields=frozenset({"file_id", "sha256"}),
    identity_field="file_id",
)
DELETION_CONFIRMED = EvidenceContract(
    type="file_absent",
    version=1,
    operation="delete",
    fields=frozenset({"cleanup_item_id"}),
    identity_field="cleanup_item_id",
)
EXISTENCE_OBSERVED = EvidenceContract(
    type="file_presence",
    version=1,
    operation="query",
    fields=frozenset({"cleanup_item_id", "present"}),
    identity_field="cleanup_item_id",
)

_CONTRACTS = EvidenceRegistry(
    (RESULT_LISTED, DIGEST_COMPUTED, DELETION_CONFIRMED, EXISTENCE_OBSERVED)
)


@dataclass(frozen=True)
class ContractFacts:
    """契约事实：类型、版本、字段集与身份成员。"""

    type: str
    version: int
    fields: frozenset[str]
    identity_field: str | None


def _facts_of(contract: EvidenceContract) -> ContractFacts:
    return ContractFacts(
        type=contract.type,
        version=contract.version,
        fields=contract.fields,
        identity_field=contract.identity_field,
    )


class ContractDriver:
    """受 ports.py 端口约束的替身驱动：返回可编排的观察与错误。"""

    def __init__(
        self,
        driver_id: str,
        declaration: DriverDeclaration,
        results: dict[str, DeviceCallResult] | None = None,
    ) -> None:
        self.driver_id = driver_id
        self.declaration = declaration
        self._results = results or {}
        self.calls: list[str] = []

    def _respond(self, operation: str) -> DeviceCallResult:
        self.calls.append(operation)
        found = self._results.get(operation)
        if found is None:
            return DeviceCallResult(observations=(), error=None)
        return found

    async def control(self, request: ControlRequest) -> DeviceCallResult:
        return self._respond("control")

    async def stop(self, request: ControlRequest) -> DeviceCallResult:
        return self._respond("stop")

    async def query_state(self, request: ControlRequest) -> DeviceCallResult:
        return self._respond("query")

    async def list_results(
        self, request: ControlRequest, batch: int
    ) -> DeviceCallResult:
        return self._respond("result")

    async def open_read(self, source, offset: int, ticket):
        self.calls.append("read")
        raise AssertionError("契约测试不派发真实读取")

    async def digest(self, request: ControlRequest) -> DeviceCallResult:
        return self._respond("digest")

    async def delete(self, request: ControlRequest) -> DeviceCallResult:
        return self._respond("delete")


class ConsumerStub:
    """按所属模块消费证据的替身：验证观察并报告接受的事实。"""

    def __init__(self, name: str, operation: str) -> None:
        self.name = name
        self.operation = operation

    def accept(self, observation: DeviceObservation, target_id: str) -> ContractFacts:
        contract = _CONTRACTS.contract(observation.type, observation.version)
        if contract.operation != self.operation:
            raise EvidenceError(
                f"{self.name} 只消费 {self.operation} 证据:"
                f" {observation.type!r} 属 {contract.operation!r}"
            )
        validate_observation(observation, contract, expected_identity=target_id)
        return _facts_of(contract)


def _full_declaration() -> DriverDeclaration:
    return DriverDeclaration(
        control_supported=True,
        stop_supported=True,
        query_supported=True,
        result_supported=True,
        read_supported=True,
        digest_supported=True,
        delete_supported=True,
    )


def _entry(
    driver: ContractDriver,
    status: DriverStatus = DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
    verified_inputs: tuple[dict[str, Any], ...] = (),
) -> DriverEntry:
    return DriverEntry(
        driver_id=driver.driver_id,
        driver=driver,
        declaration=driver.declaration,
        evidence=_CONTRACTS,
        status=status,
        verified_inputs=verified_inputs,
    )


def _observation(
    contract: EvidenceContract, identity: str, **extra: Any
) -> DeviceObservation:
    data: dict[str, Any] = {contract.identity_field: identity}
    data.update(extra)
    return DeviceObservation(type=contract.type, version=contract.version, data=data)


def _request() -> ControlRequest:
    return ControlRequest(
        operation="contract",
        binding=DeviceBinding(device_id="cam-1", driver_id="contract-double"),
        params={},
    )


async def _invoke(port, operation: str) -> DeviceCallResult:
    if operation == "result":
        return await port.list_results(_request(), batch=100)
    if operation == "digest":
        return await port.digest(_request())
    if operation == "delete":
        return await port.delete(_request())
    if operation == "query":
        return await port.query_state(_request())
    raise AssertionError(f"测试未安排该操作的调用: {operation}")


async def test_driver_declared_evidence_matches_consumers() -> None:
    """各声明的合法观察被对应消费者原样接受，事实与契约精确一致。"""
    driver = ContractDriver(
        "contract-double",
        _full_declaration(),
        results={
            "result": DeviceCallResult(
                observations=(
                    _observation(RESULT_LISTED, "7", entries=["a.mp4", "b.mp4"]),
                ),
                error=None,
            ),
            "digest": DeviceCallResult(
                observations=(_observation(DIGEST_COMPUTED, "5", sha256="00" * 32),),
                error=None,
            ),
            "delete": DeviceCallResult(
                observations=(_observation(DELETION_CONFIRMED, "3"),),
                error=None,
            ),
            "query": DeviceCallResult(
                observations=(
                    _observation(EXISTENCE_OBSERVED, "3", present=False),
                ),
                error=None,
            ),
        },
    )
    registry = DriverRegistry((_entry(driver),))
    entry = registry.entry("contract-double")
    assert entry is not None
    cases = {
        "C6": ConsumerStub("C6", "result"),
        "X4": ConsumerStub("X4", "digest"),
        "X8": ConsumerStub("X8", "delete"),
        "X8-query": ConsumerStub("X8", "query"),
    }
    observations = {
        "C6": (_observation(RESULT_LISTED, "7", entries=["a.mp4", "b.mp4"]), "7"),
        "X4": (_observation(DIGEST_COMPUTED, "5", sha256="00" * 32), "5"),
        "X8": (_observation(DELETION_CONFIRMED, "3"), "3"),
        "X8-query": (_observation(EXISTENCE_OBSERVED, "3", present=False), "3"),
    }
    accepted: dict[str, ContractFacts] = {}
    for name, consumer in cases.items():
        observation, target = observations[name]
        result = await _invoke(port_for(entry, consumer.operation), consumer.operation)
        assert result.observations == (observation,)
        accepted[name] = consumer.accept(result.observations[0], target)

    # 接受事实与契约事实精确一致：字段集、版本与身份不被消费者增删。
    assert accepted["C6"] == _facts_of(RESULT_LISTED)
    assert accepted["C6"].fields == frozenset({"activity_id", "entries"})
    assert accepted["X4"] == _facts_of(DIGEST_COMPUTED)
    assert accepted["X4"].identity_field == "file_id"
    assert accepted["X8"] == _facts_of(DELETION_CONFIRMED)
    assert accepted["X8-query"] == _facts_of(EXISTENCE_OBSERVED)


async def test_error_and_boundary_observations_are_rejected() -> None:
    """身份不符、未知类型及未知版本的观察被消费者拒绝，不解释为空。"""
    consumer = ConsumerStub("X8", "delete")
    with pytest.raises(EvidenceError):
        consumer.accept(_observation(DELETION_CONFIRMED, "4"), "3")
    with pytest.raises(EvidenceError):
        consumer.accept(
            DeviceObservation(
                type="deletion_guessed", version=1, data={"cleanup_item_id": "3"}
            ),
            "3",
        )
    with pytest.raises(EvidenceError):
        consumer.accept(
            DeviceObservation(
                type=DELETION_CONFIRMED.type,
                version=DELETION_CONFIRMED.version + 1,
                data={"cleanup_item_id": "3"},
            ),
            "3",
        )
    # 未定义成员不接受：契约字段集之外的事实进入错误处理。
    with pytest.raises(EvidenceError):
        consumer.accept(
            DeviceObservation(
                type=DELETION_CONFIRMED.type,
                version=DELETION_CONFIRMED.version,
                data={"cleanup_item_id": "3", "extra": True},
            ),
            "3",
        )


async def test_unsupported_capability_is_not_callable() -> None:
    """声明不支持的能力对消费者不可调用，也不产生设备调用。"""
    declaration = DriverDeclaration(
        control_supported=True,
        stop_supported=True,
        query_supported=False,
        result_supported=True,
        read_supported=False,
        digest_supported=True,
        delete_supported=True,
    )
    driver = ContractDriver("partial-double", declaration)
    entry = _entry(driver)
    with pytest.raises(CapabilityNotDeclaredError):
        port_for(entry, "query")
    with pytest.raises(CapabilityNotDeclaredError):
        port_for(entry, "read")
    assert driver.calls == []


async def test_call_failure_is_not_missing_capability() -> None:
    """声明支持但本次调用失败：端口仍可调用，声明不变，不降级。"""
    driver = ContractDriver(
        "failing-double",
        _full_declaration(),
        results={
            "digest": DeviceCallResult(
                observations=(), error={"code": "transport_timeout"}
            )
        },
    )
    entry = _entry(driver)
    result = await _invoke(port_for(entry, "digest"), "digest")
    assert result.error is not None
    assert result.observations == ()
    assert entry.declaration.digest_supported is True


async def test_registry_separates_software_and_device_verification() -> None:
    """软件契约状态与实际设备验收分别保存；未验收不得宣称通过。"""
    driver = ContractDriver("pending-double", _full_declaration())
    inputs = ({"operation": "digest", "identity": "5", "shape": "hex"},)
    entry = _entry(driver, status=DriverStatus.DEVICE_VERIFICATION_PENDING,
                   verified_inputs=inputs)
    registry = DriverRegistry((entry,))
    assert registry.entry("pending-double") is entry
    assert entry.status is DriverStatus.DEVICE_VERIFICATION_PENDING
    assert entry.verified_inputs == inputs
    # 能力访问不受验收状态限制；验收状态只表达证据范围。
    assert port_for(entry, "digest") is driver


async def test_unknown_operation_is_rejected() -> None:
    driver = ContractDriver("contract-double", _full_declaration())
    entry = _entry(driver)
    with pytest.raises(UnknownOperationError):
        port_for(entry, "probe")


async def test_registry_rejects_duplicate_and_missing() -> None:
    driver = ContractDriver("contract-double", _full_declaration())
    entry = _entry(driver)
    with pytest.raises(ValueError):
        DriverRegistry((entry, entry))
    assert DriverRegistry(()).entry("contract-double") is None
