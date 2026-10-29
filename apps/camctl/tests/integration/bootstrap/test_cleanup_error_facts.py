"""主机清理公开端口经真实文件任务保留错误图与可靠文件事实。"""

import os
from pathlib import Path

import pytest

from camctl.bootstrap.cleanup_assembly import HostArtifacts
from camctl.host_files.models import FilePurpose, FileRef
from camctl.host_files.tasks import FileTaskExecutor
from camctl.operations.models import AttemptTicket
from camctl.outputs.cleanup_flow import LocalArtifactRequest
from camctl.session.supervision import Supervisor

pytestmark = pytest.mark.asyncio


def _port(tmp_path):
    staging = tmp_path / "staging"
    target = staging / "derived" / "28.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original-artifact")
    request = LocalArtifactRequest("owned-db", "delete", 91,
        AttemptTicket(1, "delete", "91", "delete/91", 7),
        FileRef(28, FilePurpose.REPAIR_OUTPUT, "derived/28.bin", staging))
    executor = FileTaskExecutor(Supervisor())
    return HostArtifacts(staging, executor), request, target, executor


async def test_unlink_and_followup_inspection_failures_are_both_preserved(tmp_path, monkeypatch):
    deletion = PermissionError(13, "unlink denied")
    inspection = OSError(5, "stat after unlink failed")
    port, request, target, executor = _port(tmp_path)
    original_unlink, original_stat = Path.unlink, os.stat
    unlink_calls = []

    def unlink(path, *args, **kwargs):
        if path == target:
            unlink_calls.append(path)
            raise deletion
        return original_unlink(path, *args, **kwargs)

    def stat(path, *args, **kwargs):
        if unlink_calls and isinstance(path, (str, os.PathLike)) and Path(path) == target:
            raise inspection
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", unlink)
        fault.setattr(os, "stat", stat)
        result = await port.delete(request)
        settled = await executor.drain_required()
        assert not settled.failed
    assert unlink_calls == [target] and target.read_bytes() == b"original-artifact"
    assert result.observations == ()
    error = result.error["details"]["diagnostic"]
    assert error["type"] == "builtins.ExceptionGroup"
    assert [leaf["message"] for leaf in error["exceptions"]] == [str(deletion), str(inspection)]


async def test_failed_unlink_followed_by_absence_keeps_both_error_and_fact(tmp_path, monkeypatch):
    failure = PermissionError(13, "unlink completion uncertain")
    port, request, target, executor = _port(tmp_path)
    original_unlink = Path.unlink
    unlink_calls = []

    def unlink(path, *args, **kwargs):
        if path == target:
            unlink_calls.append(path)
            original_unlink(path, *args, **kwargs)
            raise failure
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", unlink)
        result = await port.delete(request)
        settled = await executor.drain_required()
        assert not settled.failed
    assert unlink_calls == [target] and not target.exists()
    assert result.observations[0].type == "file_absent"
    assert result.observations[0].data == {"cleanup_item_id": "91"}
    assert result.error["details"]["diagnostic"]["type"] == "builtins.PermissionError"
    assert result.error["details"]["diagnostic"]["errno"] == 13
