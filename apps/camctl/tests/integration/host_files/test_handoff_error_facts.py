"""公开文件交接入口在真实文件上保存原异常及交接事实。"""
import errno
import os
import sys

from .file_boundary_helpers import fail_fd_operation

import pytest

from camctl.host_files import handoff
from camctl.host_files.io import DirectorySyncStage


def _publication(tmp_path):
    staging, ready = tmp_path / 'staging', tmp_path / 'ready'
    staging.mkdir(); ready.mkdir()
    source = staging / 'source'
    source.write_bytes(b'original staged bytes')
    return source, handoff.HandoffDirectories(staging, ready)


@pytest.mark.skipif(sys.platform == "win32", reason="链接删除和目录同步不适用于 Windows")
def test_publish_preserves_unlink_and_directory_sync_errors(tmp_path, monkeypatch):
    source, directories = _publication(tmp_path)
    remove = OSError(errno.EIO, 'source unlink failed', str(source))
    sync = OSError(errno.ENOSPC, 'directory sync failed', str(source.parent))
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if path == source:
            raise remove
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', unlink)
    fail_fd_operation(monkeypatch, 'fsync', source.parent, sync)

    result = handoff.publish_staged_file_sync(source, directories, handoff.ReadyName('target'))

    assert result.stage is handoff.PublishStage.MOVED
    assert result.directory is DirectorySyncStage.FAILED
    assert result.source_removed is False
    assert isinstance(result.exception, ExceptionGroup)
    assert result.exception.exceptions == (remove, sync)
    assert (remove.errno, remove.filename) == (errno.EIO, str(source))
    assert (sync.errno, sync.filename) == (errno.ENOSPC, str(source.parent))
    assert source.read_bytes() == (directories.ready / 'target').read_bytes() == b'original staged bytes'


def test_publish_success_retains_failed_target_inspection(tmp_path, monkeypatch):
    source, directories = _publication(tmp_path)
    target = directories.ready / 'target'
    inspect = PermissionError(errno.EACCES, 'target stat failed', str(target))
    real_stat = os.stat
    def inspect_target(path, *args, **kwargs):
        if path == target:
            raise inspect
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', inspect_target)

    result = handoff.publish_staged_file_sync(source, directories, handoff.ReadyName('target'))

    assert result.stage is handoff.PublishStage.MOVED
    assert result.directory is (DirectorySyncStage.UNSUPPORTED if sys.platform == "win32"
                                else DirectorySyncStage.SYNCED)
    assert result.exception is inspect
    assert (result.exception.errno, result.exception.filename) == (errno.EACCES, str(target))
    assert not source.exists()
    assert (directories.ready / 'target').read_bytes() == b'original staged bytes'


def test_withdraw_unknown_preserves_interruption(tmp_path, monkeypatch):
    target = tmp_path / 'target'
    target.write_bytes(b'camctl owned bytes')
    failure = InterruptedError(errno.EINTR, 'unlink interrupted', str(target))
    real_remove = os.remove
    def remove(path, *args, **kwargs):
        if path == target:
            raise failure
        return real_remove(path, *args, **kwargs)
    monkeypatch.setattr(os, 'remove', remove)
    result = handoff.withdraw_ready_file(handoff.HandoffIdentity(tmp_path, 'target'))
    assert result.stage is handoff.WithdrawStage.UNKNOWN
    assert result.exception is failure
    assert (result.exception.errno, result.exception.filename) == (errno.EINTR, str(target))
    assert target.read_bytes() == b'camctl owned bytes'


def test_absent_withdrawal_has_no_error(tmp_path):
    result = handoff.withdraw_ready_file(handoff.HandoffIdentity(tmp_path, 'target'))
    assert result.stage is handoff.WithdrawStage.NOT_PRESENT
    assert result.exception is None
    assert not (tmp_path / 'target').exists()
