"""主机正式源清理结果保留实际错误图及可靠文件事实。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from camctl.bootstrap import cleanup_assembly
from camctl.host_files.models import FileObservation, FileObservationKind


def test_unlink_and_followup_inspection_failures_are_both_preserved(monkeypatch):
    deletion = PermissionError(13, 'unlink denied')
    inspection = OSError(5, 'stat after unlink failed')
    path = Path('/staging/source')
    monkeypatch.setattr(cleanup_assembly, 'observe_file', Mock(side_effect=[
        FileObservation(FileObservationKind.VALID_OBJECT, path),
        FileObservation(FileObservationKind.ERROR, path, error=inspection)]))
    target = Mock()
    target.unlink.side_effect = deletion
    monkeypatch.setattr(cleanup_assembly, 'resolve_file', Mock(return_value=SimpleNamespace(path=target)))
    request = SimpleNamespace(file_ref=None, operation='delete', cleanup_item_id=91)

    result = cleanup_assembly.HostArtifacts(Path('/staging'))._execute(request)

    assert result.observations == ()
    error = result.error['details']['diagnostic']
    assert error['type'] == 'builtins.ExceptionGroup'
    assert [leaf['message'] for leaf in error['exceptions']] == [str(deletion), str(inspection)]


def test_failed_unlink_followed_by_absence_keeps_both_error_and_fact(monkeypatch):
    failure = PermissionError(13, 'unlink completion uncertain')
    path = Path('/staging/source')
    monkeypatch.setattr(cleanup_assembly, 'observe_file', Mock(side_effect=[
        FileObservation(FileObservationKind.VALID_OBJECT, path),
        FileObservation(FileObservationKind.MISSING, path)]))
    target = Mock()
    target.unlink.side_effect = failure
    monkeypatch.setattr(cleanup_assembly, 'resolve_file', Mock(return_value=SimpleNamespace(path=target)))
    request = SimpleNamespace(file_ref=None, operation='delete', cleanup_item_id=91)

    result = cleanup_assembly.HostArtifacts(Path('/staging'))._execute(request)

    assert result.observations[0].type == 'file_absent'
    assert result.error['details']['diagnostic']['type'] == 'builtins.PermissionError'
    assert result.error['details']['diagnostic']['errno'] == 13
