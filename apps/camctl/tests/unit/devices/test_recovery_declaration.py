"""驱动逐操作声明普通前台命令的恢复适用范围。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from camctl.devices.ports import DriverDeclaration
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.capture.recovery import recovery_registry


def _declaration():
    return DriverDeclaration(True, True, True, True, False, False, True)


def test_omitted_recovery_declaration_does_not_authorize_any_operation():
    assert _declaration().adb_foreground_recovery_operations == frozenset()


def test_recovery_declaration_preserves_explicit_operation_scope():
    declaration = replace(_declaration(), adb_foreground_recovery_operations=frozenset({"stop", "query"}))

    assert declaration.adb_foreground_recovery_operations == frozenset({"stop", "query"})


@pytest.mark.parametrize("operations", [frozenset({"future_operation"}), {"control"}])
def test_recovery_declaration_rejects_unknown_or_mutable_operation_scope(operations):
    with pytest.raises(ValueError):
        replace(_declaration(), adb_foreground_recovery_operations=operations)


def test_recovery_keeps_original_return_contracts_and_rebinds_only_recovery_template():
    result = EvidenceContract("results_returned", 1, "result", frozenset())
    control = EvidenceContract("start_confirmed", 1, "control", frozenset({"activity_id"}),
                               identity_field="activity_id")
    template = EvidenceContract("adb_foreground_recovery", 1, "control", frozenset())
    original = EvidenceRegistry((template, result, control))
    entry = SimpleNamespace(declaration=replace(_declaration(),
        adb_foreground_recovery_operations=frozenset({"result"})), evidence=original)

    recovered = recovery_registry(entry, "result")

    assert recovered.contract("results_returned", 1) == result
    assert recovered.contract("start_confirmed", 1) == control
    assert recovered.contract("adb_foreground_recovery", 1).operation == "result"
    assert original.contract("adb_foreground_recovery", 1) == template
    assert recovery_registry(entry, "control") is None
