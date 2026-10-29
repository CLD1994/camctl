"""驱动运行登记的本进程状态分区，不打开业务资源。"""

import pytest
import sys
from types import ModuleType, SimpleNamespace

from camctl.devices.drivers.registry import DriverEntry, DriverStatus
from camctl.devices.drivers.runtime import current_registry, register_drivers, reset_drivers
from camctl.devices.evidence import EvidenceRegistry
from camctl.devices.ports import DriverDeclaration


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    definitions = ModuleType("camctl.devices.definitions_runtime")
    definitions.current_driver_definitions = lambda: SimpleNamespace(drivers={})
    monkeypatch.setitem(sys.modules, definitions.__name__, definitions)
    reset_drivers()
    yield
    reset_drivers()


class TestDriverRuntimeRegistry:
    def test_registered_entry_is_resolved_and_duplicates_rejected(self):
        def entry():
            return DriverEntry(
                driver_id="production-double", driver=object(),
                declaration=DriverDeclaration(
                    control_supported=False, stop_supported=False, query_supported=False,
                    result_supported=False, read_supported=False, digest_supported=False,
                    delete_supported=False),
                evidence=EvidenceRegistry(()),
                status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED)

        first = entry()
        assert current_registry().entry(first.driver_id) is None
        register_drivers(first)
        assert current_registry().entry(first.driver_id) is first
        with pytest.raises(ValueError, match="重复登记"):
            register_drivers(entry())
        reset_drivers()
        assert current_registry().entry(first.driver_id) is None
