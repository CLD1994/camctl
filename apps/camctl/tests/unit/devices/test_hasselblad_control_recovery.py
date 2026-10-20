"""本地 control 恢复与 READ、ADB 的责任分别登记。"""
from types import SimpleNamespace
from camctl.devices.drivers.hasselblad.driver import EVIDENCE
from camctl.devices.drivers.hasselblad.definitions import DRIVER_DECLARATION
from camctl.devices.ports import DriverDeclaration
from camctl.operations.recovery import recovery_registry


def test_hasselblad_control_recovery_has_explicit_empty_evidence_contract():
    registry = recovery_registry(SimpleNamespace(declaration=DRIVER_DECLARATION,evidence=EVIDENCE),'control')
    assert registry is not None
    assert registry.contract('local_control_recovery',1).fields == frozenset()


def test_other_driver_control_does_not_gain_local_recovery():
    declaration = DriverDeclaration(True,False,False,False,False,False,False)
    assert recovery_registry(SimpleNamespace(declaration=declaration,evidence=EVIDENCE),'control') is None


def test_hasselblad_read_keeps_its_distinct_recovery_evidence():
    registry = recovery_registry(SimpleNamespace(declaration=DRIVER_DECLARATION,evidence=EVIDENCE),'read')
    assert registry.contract('local_read_recovery',1).operation == 'read'


def test_declared_local_control_without_contract_remains_blocked():
    from camctl.devices.evidence import EvidenceRegistry
    entry = SimpleNamespace(declaration=DRIVER_DECLARATION,evidence=EvidenceRegistry(()))
    assert recovery_registry(entry,'control') is None
