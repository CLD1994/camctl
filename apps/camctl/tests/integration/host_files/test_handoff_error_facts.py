"""公开文件交接入口在真实文件上保存原异常及交接事实。"""
from unittest.mock import Mock

import pytest

from camctl.host_files import handoff
from camctl.host_files.io import DirectorySyncStage


def _publication(tmp_path):
    staging, ready = tmp_path / 'staging', tmp_path / 'ready'
    staging.mkdir(); ready.mkdir()
    source = staging / 'source'
    source.write_bytes(b'original staged bytes')
    return source, handoff.HandoffDirectories(staging, ready)


def test_publish_preserves_unlink_and_directory_sync_errors(tmp_path, monkeypatch):
    source, directories = _publication(tmp_path)
    remove = OSError(5, 'source unlink failed')
    sync = OSError(28, 'ready directory sync failed')
    monkeypatch.setattr(handoff, '_unlink', Mock(side_effect=remove))
    monkeypatch.setattr(handoff, '_sync_directory', Mock(side_effect=sync))

    result = handoff.publish_staged_file_sync(source, directories, handoff.ReadyName('target'))

    assert result.stage is handoff.PublishStage.MOVED
    assert result.directory is DirectorySyncStage.FAILED
    assert result.source_removed is False
    assert isinstance(result.exception, ExceptionGroup)
    assert result.exception.exceptions == (remove, sync)
    assert source.read_bytes() == (directories.ready / 'target').read_bytes() == b'original staged bytes'


def test_publish_success_retains_failed_target_inspection(tmp_path, monkeypatch):
    source, directories = _publication(tmp_path)
    inspect = PermissionError(13, 'target stat failed')
    stat = handoff._stat
    def inspect_target(path):
        if path == directories.ready / 'target':
            raise inspect
        return stat(path)
    monkeypatch.setattr(handoff, '_stat', inspect_target)

    result = handoff.publish_staged_file_sync(source, directories, handoff.ReadyName('target'))

    assert result.stage is handoff.PublishStage.MOVED
    assert result.directory is DirectorySyncStage.SYNCED
    assert result.exception is inspect
    assert not source.exists()
    assert (directories.ready / 'target').read_bytes() == b'original staged bytes'


def test_withdraw_unknown_preserves_interruption(tmp_path, monkeypatch):
    target = tmp_path / 'target'
    target.write_bytes(b'camctl owned bytes')
    failure = InterruptedError('unlink interrupted')
    monkeypatch.setattr(handoff, '_remove', Mock(side_effect=failure))
    result = handoff.withdraw_ready_file(handoff.HandoffIdentity(tmp_path, 'target'))
    assert result.stage is handoff.WithdrawStage.UNKNOWN
    assert result.exception is failure
    assert target.read_bytes() == b'camctl owned bytes'


def test_absent_withdrawal_has_no_error(tmp_path):
    result = handoff.withdraw_ready_file(handoff.HandoffIdentity(tmp_path, 'target'))
    assert result.stage is handoff.WithdrawStage.NOT_PRESENT
    assert result.exception is None
    assert not (tmp_path / 'target').exists()
