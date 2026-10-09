"""驱动逐操作声明普通前台命令的恢复适用范围。"""

from dataclasses import replace

import pytest

from camctl.devices.ports import DriverDeclaration


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
