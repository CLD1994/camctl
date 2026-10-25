"""执行会话固定恢复边界前重置全部内置相机的准备凭据。"""

from types import SimpleNamespace
from unittest.mock import Mock, MagicMock

from camctl.bootstrap.lifecycle import _initialize_recovery
from camctl.devices.drivers.adb_cameras import registration as adb
from camctl.devices.drivers.hasselblad import registration as hasselblad
from camctl.persistence.runtime import CURRENT_FORMAT_VERSION


def test_execution_session_resets_adb_and_hasselblad_preparation(monkeypatch):
    begin_adb, begin_hasselblad = Mock(), Mock()
    monkeypatch.setattr(adb, "begin_builtin_camera_session", begin_adb)
    monkeypatch.setattr(hasselblad, "begin_builtin_hasselblad_session", begin_hasselblad)
    cursor = MagicMock()
    cursor.__iter__.return_value = iter(())
    cursor.fetchone.return_value = (42,)
    owned = SimpleNamespace(
        metadata=SimpleNamespace(format_version=CURRENT_FORMAT_VERSION),
        connection=SimpleNamespace(execute=Mock(return_value=cursor)))
    deps = SimpleNamespace(work_files=None, recovery_max_event_id=None)

    _initialize_recovery(deps, owned)

    begin_adb.assert_called_once_with()
    begin_hasselblad.assert_called_once_with()
    assert deps.recovery_max_event_id == 42
