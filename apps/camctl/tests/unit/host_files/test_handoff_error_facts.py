"""文件交接把真实系统错误交回协程，同时保持原交接事实。"""
from pathlib import Path
from unittest.mock import Mock

import pytest

from camctl.host_files import handoff
from camctl.host_files.io import DirectorySyncStage


def _publication(monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda path: path)
    monkeypatch.setattr(handoff, "_stat", Mock(side_effect=[object(), FileNotFoundError()]))
    monkeypatch.setattr(handoff, "_RENAME_WITHOUT_OVERWRITE", False)
    monkeypatch.setattr(handoff, "_DIRECTORY_SYNC_SUPPORTED", True)
    monkeypatch.setattr(handoff, "_link", Mock())
    monkeypatch.setattr(handoff, "_unlink", Mock())
    monkeypatch.setattr(handoff, "_sync_directory", Mock())
    return Path('/staging/source'), handoff.HandoffDirectories(Path('/staging'), Path('/ready'))


def test_publish_preserves_unlink_and_directory_sync_errors(monkeypatch):
    source, directories = _publication(monkeypatch)
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


def test_publish_success_retains_failed_target_inspection(monkeypatch):
    source, directories = _publication(monkeypatch)
    inspect = PermissionError(13, 'target stat failed')
    monkeypatch.setattr(handoff, '_stat', Mock(side_effect=[object(), inspect]))

    result = handoff.publish_staged_file_sync(source, directories, handoff.ReadyName('target'))

    assert result.stage is handoff.PublishStage.MOVED
    assert result.directory is DirectorySyncStage.SYNCED
    assert result.exception is inspect


def test_withdraw_unknown_preserves_interruption(monkeypatch):
    failure = InterruptedError('unlink interrupted')
    monkeypatch.setattr(handoff, '_remove', Mock(side_effect=failure))
    result = handoff.withdraw_ready_file(handoff.HandoffIdentity(Path('/ready'), 'target'))
    assert result.stage is handoff.WithdrawStage.UNKNOWN
    assert result.exception is failure


def test_absent_withdrawal_has_no_error(monkeypatch):
    monkeypatch.setattr(handoff, '_remove', Mock(side_effect=FileNotFoundError()))
    result = handoff.withdraw_ready_file(handoff.HandoffIdentity(Path('/ready'), 'target'))
    assert result.stage is handoff.WithdrawStage.NOT_PRESENT
    assert result.exception is None
