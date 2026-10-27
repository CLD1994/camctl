"""可用性恢复只消费驱动为该操作登记的本地收场依据。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from camctl import resources
from camctl.devices.evidence import EvidenceContract, EvidenceError
from camctl.operations.recovery import recovery_registry


@pytest.fixture(autouse=True)
def no_package_resources(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("不能读取包资源"))


def test_availability_recovery_uses_its_own_contract():
    contract = EvidenceContract("local_availability_recovery", 1, "availability", frozenset())
    evidence = SimpleNamespace(contract=Mock(return_value=contract), with_contract=Mock(return_value="registry"))
    entry = SimpleNamespace(declaration=SimpleNamespace(local_availability_recovery_supported=True), evidence=evidence)

    assert recovery_registry(entry, "availability") == "registry"
    evidence.contract.assert_called_once_with("local_availability_recovery", 1)
    assert evidence.with_contract.call_args.args[0].operation == "availability"


def test_availability_recovery_requires_a_registered_contract():
    evidence = SimpleNamespace(contract=Mock(side_effect=EvidenceError("missing")))
    entry = SimpleNamespace(declaration=SimpleNamespace(local_availability_recovery_supported=True), evidence=evidence)

    assert recovery_registry(entry, "availability") is None


def test_availability_recovery_requires_driver_declaration():
    evidence = SimpleNamespace(contract=Mock())
    entry = SimpleNamespace(declaration=SimpleNamespace(), evidence=evidence)

    assert recovery_registry(entry, "availability") is None
    evidence.contract.assert_not_called()
