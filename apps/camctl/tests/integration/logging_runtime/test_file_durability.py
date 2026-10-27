"""真实文件处理器的同步事实、失败分类与正式名字保护。"""

import json
import os
import stat
from pathlib import Path

import pytest

from camctl.logging_runtime.clh_adapter import FileChannel, WriteFailureStage
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord


def record(message="trigger", level=LogLevel.ERROR):
    return LogRecord(level, message)


def messages(path):
    return [json.loads(line)["message"] for line in path.read_text().splitlines()]


def test_error_flushes_before_file_sync_and_syncs_complete_directory_chain(tmp_path, monkeypatch):
    path = tmp_path / "new-parent" / "logs" / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original = os.fsync
    synced = []

    def fsync(fd):
        synced.append((stat.S_ISREG(os.fstat(fd).st_mode), Path(os.readlink(f"/proc/self/fd/{fd}"))))
        if synced[-1][0]:
            assert messages(path) == ["trigger"]
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    try:
        result = channel.write_record(record())
        assert result.appended and result.durable
        assert result.completion.delivered_for(LogLevel.ERROR)
        assert synced[0] == (True, path)
        expected = [path.parent, *path.parent.parents]
        assert [name for regular, name in synced if not regular] == expected
    finally:
        channel.close()


def test_info_needs_flush_without_file_sync(tmp_path, monkeypatch):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    calls = []
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))
    try:
        result = channel.write_record(record(level=LogLevel.INFO))
        assert result.appended and not result.durable
        assert result.completion.delivered_for(LogLevel.INFO)
        assert calls == []
        assert messages(path) == ["trigger"]
    finally:
        channel.close()


@pytest.mark.parametrize("fail_directory", [False, True])
def test_sync_failure_preserves_append_and_disables_without_retry(tmp_path, monkeypatch, fail_directory):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original = os.fsync
    attempts = []

    def fsync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        attempts.append(directory)
        if directory is fail_directory:
            raise OSError("sync denied")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    try:
        result = channel.write_record(record())
        assert result.appended and not result.durable
        assert not result.completion.delivered_for(LogLevel.ERROR)
        assert result.failure_stage is (WriteFailureStage.DIRECTORY_SYNC if fail_directory
                                        else WriteFailureStage.FILE_SYNC)
        assert result.sync_error and result.append_error is None
        assert channel.disabled and messages(path) == ["trigger"]
        before = list(attempts)
        assert not channel.write_record(record("later")).appended
        assert attempts == before
    finally:
        channel.close()


@pytest.mark.parametrize("fail_directory", [False, True])
def test_original_sync_failure_does_not_start_copy(tmp_path, monkeypatch, fail_directory):
    path, destination = tmp_path / "camctl.log", tmp_path / "copy.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) is fail_directory:
            raise OSError("original sync denied")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    try:
        result, copy_error = channel.copy_with_write(record(), destination)
        assert result.appended and not result.durable
        assert result.sync_error and result.copy_handles_closed
        assert not destination.exists() and copy_error is None
        assert messages(path) == ["trigger"]
    finally:
        channel.close()


def test_copy_target_sync_failure_keeps_original_durable(tmp_path, monkeypatch):
    path, destination = tmp_path / "camctl.log", tmp_path / "copy.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original = os.fsync

    def fsync(fd):
        if Path(os.readlink(f"/proc/self/fd/{fd}")) == destination:
            raise OSError("copy sync denied")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    try:
        result, copy_error = channel.copy_with_write(record(), destination)
        assert result.appended and result.durable
        assert result.sync_error is None and copy_error
        assert not channel.disabled and result.copy_handles_closed
    finally:
        channel.close()
