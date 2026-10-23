"""相机设置工具退出 0 也不能覆盖明确的服务不可用诊断。"""

import pytest

from camctl.devices.drivers.adb_cameras.responses import Action6SimulationSettingInterpreter
from camctl.devices.drivers.adb_cameras.osmo360ii_responses import Osmo360IISimulationInterpreter
from camctl.operations.models import EffectState
from camctl.operations.process import LocalExit, RawToolOutcome


@pytest.mark.parametrize("interpreter", [Action6SimulationSettingInterpreter, Osmo360IISimulationInterpreter])
@pytest.mark.parametrize("message", [
    b'SIMULATE:[E]:connect_to_server(54): "SimuDev: get correct server service failed rlt 0"\n',
    b'SIMULATE:[E]:control_set_data(93): "SimuDev: when send control set data, server not exist!"\n',
    b'SIMULATE:[E]:control_notify_data(117): "SimuDev: when send control notify data, server not exist!"\n',
])
def test_explicit_service_failure_is_not_setting_acceptance(interpreter, message):
    raw = RawToolOutcome(LocalExit(exit_code=0), message, None, None, stderr=b"")

    result = interpreter("7").interpret(raw)

    assert result.effect is EffectState.UNKNOWN
    assert result.observations == ()
    assert result.error.stage == "device_settings"
    assert result.error.details["reason"] == "simulation_service_unavailable"
