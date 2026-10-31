"""真实文件处理器的同步事实、失败分类与正式名字保护。"""

import json
import os
import stat
import asyncio
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


class InterruptedRotation(BaseException):
    pass


def formal_contents(path):
    """只从当前及正式数字归档读取身份，临时名字不能充当保留证明。"""
    files = [path, *[item for item in path.parent.glob(path.name + ".*")
                    if item.name[len(path.name) + 1:].isdecimal()]]
    identities = {}
    for item in files:
        if item.exists():
            info = item.stat()
            identities.setdefault((info.st_dev, info.st_ino), messages(item))
    return identities


def observe_metadata(monkeypatch, path, protected, stop_at):
    count = 0
    originals = {name: getattr(os, name) for name in ("link", "unlink", "rename", "fsync")}

    def observe(name, *args, **kwargs):
        nonlocal count
        relevant = (stat.S_ISDIR(os.fstat(args[0]).st_mode)
                    and (os.fstat(args[0]).st_dev, os.fstat(args[0]).st_ino)
                    == (path.parent.stat().st_dev, path.parent.stat().st_ino)) if name == "fsync" else (
                        Path(args[0]).parent == path.parent)
        value = originals[name](*args, **kwargs)
        if relevant:
            assert protected <= {message for group in formal_contents(path).values() for message in group}
            count += 1
            if count == stop_at:
                raise InterruptedRotation(f"metadata-{count}")
        return value

    for name in originals:
        monkeypatch.setattr(os, name, lambda *args, _name=name, **kwargs: observe(_name, *args, **kwargs))


@pytest.mark.parametrize("stop_at", range(1, 15))
def test_info_rotation_preserves_formal_names_at_every_metadata_cut(tmp_path, monkeypatch, stop_at):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=1, file_count=4)
    for name in ("oldest", "older", "newer", "current"):
        assert channel.write_record(record(name)).appended
    protected = {"older", "newer", "current"}
    with monkeypatch.context() as patch:
        observe_metadata(patch, path, protected, stop_at)
        with pytest.raises(InterruptedRotation):
            channel.write_record(record("interrupted", LogLevel.INFO))
    channel.close()
    reopened = FileChannel(path, max_bytes=1, file_count=4)
    try:
        result = reopened.write_record(record("recovered"))
        assert result.appended and result.durable
        groups = formal_contents(path)
        assert {message for group in groups.values() for message in group} == protected | {"recovered"}
        assert len(groups) == 4
        assert len([item for item in path.parent.iterdir() if item.name.startswith(path.name)]) == 4
    finally:
        reopened.close()


@pytest.mark.parametrize("stop_at", range(1, 12))
def test_recovery_can_itself_be_interrupted_without_double_eviction(tmp_path, monkeypatch, stop_at):
    path = tmp_path / "camctl.log"
    for suffix, name in (("", "current"), (".1", "newer"), (".4", "older")):
        Path(str(path) + suffix).write_text(json.dumps({"message": name}) + "\n")
    os.link(Path(str(path) + ".1"), Path(str(path) + ".2"))
    protected = {"current", "newer", "older"}
    channel = FileChannel(path, max_bytes=1, file_count=4)
    with monkeypatch.context() as patch:
        observe_metadata(patch, path, protected, stop_at)
        with pytest.raises(InterruptedRotation):
            channel.write_record(record("interrupted", LogLevel.INFO))
    channel.close()
    reopened = FileChannel(path, max_bytes=1, file_count=4)
    try:
        result = reopened.write_record(record("recovered"))
        assert result.durable
        groups = formal_contents(path)
        assert {message for group in groups.values() for message in group} == protected | {"recovered"}
        assert len(groups) == 4
    finally:
        reopened.close()


@pytest.mark.parametrize("via_copy", [False, True])
def test_current_and_first_archive_same_identity_finishes_existing_rotation(tmp_path, via_copy):
    path = tmp_path / "camctl.log"
    for suffix, name in (("", "current"), (".2", "newer"), (".3", "older")):
        Path(str(path) + suffix).write_text(json.dumps({"message": name}) + "\n")
    os.link(path, Path(str(path) + ".1"))
    channel = FileChannel(path, max_bytes=1, file_count=4)
    try:
        if via_copy:
            destination = tmp_path / "copy.log"
            result, copy_error = channel.copy_with_write(record("recovered"), destination)
            assert copy_error is None and messages(destination) == ["recovered"]
        else:
            result = channel.write_record(record("recovered"))
        assert result.durable
        assert {message for group in formal_contents(path).values() for message in group} == {
            "current", "newer", "older", "recovered"}
    finally:
        channel.close()


@pytest.mark.parametrize("control", [False, True])
def test_directory_close_failure_keeps_all_completed_sync_facts(tmp_path, monkeypatch, control):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original_close = os.close
    failed = False
    original = asyncio.CancelledError("directory close control") if control else OSError("directory close denied")

    def close(fd):
        nonlocal failed
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        original_close(fd)
        if directory and not failed:
            failed = True
            raise original

    monkeypatch.setattr(os, "close", close)
    try:
        if control:
            with pytest.raises(asyncio.CancelledError) as raised:
                channel.write_record(record())
            assert raised.value is original
            result = original.write_result
        else:
            result = channel.write_record(record())
        assert result.appended and result.durable
        assert result.close_error and result.sync_error is None
        assert result.failure_stage is WriteFailureStage.DIRECTORY_CLOSE
        assert channel.disabled
    finally:
        channel.close()


@pytest.mark.parametrize("directory", [False, True])
def test_sync_control_retains_original_exception_and_completed_append(tmp_path, monkeypatch, directory):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original_fsync = os.fsync
    original = asyncio.CancelledError("sync control")

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) is directory:
            raise original
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            channel.write_record(record())
        assert raised.value is original
        assert original.write_result.appended and not original.write_result.durable
        assert original.write_result.sync_error and channel.disabled
        assert messages(path) == ["trigger"]
    finally:
        channel.close()


@pytest.mark.parametrize("operation", ["link", "unlink"])
@pytest.mark.parametrize("via_copy", [False, True])
def test_recovery_move_failure_still_appends_current_without_new_rotation(tmp_path, monkeypatch, operation, via_copy):
    path = tmp_path / "camctl.log"
    for suffix, name in (("", "current"), (".1", "newer"), (".3", "older")):
        Path(str(path) + suffix).write_text(json.dumps({"message": name}) + "\n")
    channel = FileChannel(path, max_bytes=1, file_count=4)
    original = getattr(os, operation)

    def fail_recovery(source, *args, **kwargs):
        if Path(source) == Path(str(path) + ".3"):
            raise OSError("archive recovery denied")
        return original(source, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, operation, fail_recovery)
            if via_copy:
                destination = tmp_path / "copy.log"
                result, copy_error = channel.copy_with_write(record("continued"), destination)
                assert copy_error is None
                assert messages(destination) == ["current", "continued"]
            else:
                result = channel.write_record(record("continued"))
            assert result.appended and result.durable
            assert result.rotation_error and result.failure_stage is None
            assert not channel.disabled and messages(path) == ["current", "continued"]
        assert channel.write_record(record("later")).durable
        assert {message for group in formal_contents(path).values() for message in group} == {
            "current", "newer", "older", "continued", "later"}
    finally:
        channel.close()


@pytest.mark.parametrize("via_copy", [False, True])
def test_directory_close_control_is_preserved_when_later_sync_fails(tmp_path, monkeypatch, via_copy):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original_close, original_sync = os.close, os.fsync
    original = asyncio.CancelledError("first directory close")
    closed = False

    def close(fd):
        nonlocal closed
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        original_close(fd)
        if directory and not closed:
            closed = True
            raise original

    def fsync(fd):
        if closed and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("later parent sync failed")
        return original_sync(fd)

    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(os, "fsync", fsync)
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            if via_copy:
                channel.copy_with_write(record(), tmp_path / "copy.log")
            else:
                channel.write_record(record())
        assert raised.value is original
        result = original.write_result
        assert result.appended and not result.durable
        assert result.sync_error and result.close_error
        assert result.failure_stage is WriteFailureStage.DIRECTORY_SYNC
        assert channel.disabled
    finally:
        channel.close()


@pytest.mark.parametrize("via_copy", [False, True])
def test_rotation_directory_sync_failure_disables_before_new_append(tmp_path, monkeypatch, via_copy):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=1, file_count=4)
    for name in ("oldest", "older", "newer", "current"):
        assert channel.write_record(record(name)).durable
    original = os.fsync
    attempts = 0

    def fsync(fd):
        nonlocal attempts
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            attempts += 1
            raise OSError("rotation directory sync denied")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    try:
        if via_copy:
            destination = tmp_path / "copy.log"
            result, copy_error = channel.copy_with_write(record("new"), destination)
            assert copy_error is None and not destination.exists()
        else:
            result = channel.write_record(record("new", LogLevel.INFO))
        assert not result.appended and not result.durable
        assert result.failure_stage is WriteFailureStage.DIRECTORY_SYNC
        assert result.sync_error and result.append_error is None
        assert channel.disabled and attempts == 1
        assert {"older", "newer", "current"} <= {
            message for group in formal_contents(path).values() for message in group}
        assert not channel.write_record(record("later")).appended
        assert attempts == 1
    finally:
        channel.close()


@pytest.mark.parametrize("file_count", [1, 2, 4])
def test_normal_retention_keeps_latest_allowed_identities(tmp_path, file_count):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=1, file_count=file_count)
    try:
        for index in range(8):
            assert channel.write_record(record(str(index))).durable
        groups = formal_contents(path)
        assert len(groups) == file_count
        assert {message for group in groups.values() for message in group} == {
            str(index) for index in range(8 - file_count, 8)}
    finally:
        channel.close()


def test_reduced_retention_cleans_oldest_without_changing_unrelated_files(tmp_path):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=1, file_count=4)
    for name in ("oldest", "older", "newer", "current"):
        assert channel.write_record(record(name)).durable
    channel.close()
    unrelated = tmp_path / "camctl.log.notes"
    unrelated.write_text("preserved")
    reduced = FileChannel(path, max_bytes=4096, file_count=2)
    try:
        assert reduced.write_record(record("continued", LogLevel.INFO)).appended
        assert {message for group in formal_contents(path).values() for message in group} == {
            "current", "continued", "newer"}
        assert len(formal_contents(path)) == 2 and unrelated.read_text() == "preserved"
    finally:
        reduced.close()


def test_unsupported_directory_sync_cannot_confirm_durable(tmp_path, monkeypatch):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    try:
        with monkeypatch.context() as patch:
            patch.delattr(os, "O_DIRECTORY")
            result = channel.write_record(record())
        assert result.appended and not result.durable
        assert result.failure_stage is WriteFailureStage.DIRECTORY_SYNC
        assert result.sync_error and channel.disabled
    finally:
        channel.close()


@pytest.mark.parametrize("via_copy", [False, True])
def test_unlock_control_preserves_durable_record_result(tmp_path, monkeypatch, via_copy):
    import fcntl
    import portalocker.portalocker as portalocker_api

    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original_flock = fcntl.flock
    original = asyncio.CancelledError("unlock control")
    interrupted = False

    def flock(fd, operation):
        nonlocal interrupted
        value = original_flock(fd, operation)
        if operation == fcntl.LOCK_UN and not interrupted:
            interrupted = True
            raise original
        return value

    monkeypatch.setattr(portalocker_api, "LOCKER", flock)
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            if via_copy:
                channel.copy_with_write(record(), tmp_path / "copy.log")
            else:
                channel.write_record(record())
        assert raised.value is original
        assert original.write_result.appended and original.write_result.durable
        assert original.write_result.failure_stage is WriteFailureStage.LOCK_RELEASE
        assert original.write_result.coordination_error and channel.disabled
    finally:
        channel.close()


@pytest.mark.parametrize("via_copy", [False, True])
@pytest.mark.parametrize("sync_control", [False, True])
def test_disable_close_control_retains_first_control_and_append(tmp_path, monkeypatch, via_copy, sync_control):
    import concurrent_log_handler

    path = tmp_path / "camctl.log"
    path.touch()
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original_open = concurrent_log_handler._open
    first = asyncio.CancelledError("sync control") if sync_control else OSError("sync failed")
    close_control = asyncio.CancelledError("handler close control")
    close_attempts = 0

    class Stream:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def close(self):
            nonlocal close_attempts
            was_open = not self.wrapped.closed
            self.wrapped.close()
            if was_open:
                close_attempts += 1
                if close_attempts == 1:
                    raise close_control

    def open_file(name, *args, **kwargs):
        value = original_open(name, *args, **kwargs)
        return Stream(value) if str(name) == str(path) else value

    monkeypatch.setattr(concurrent_log_handler, "_open", open_file)
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(first))
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            if via_copy:
                channel.copy_with_write(record(), tmp_path / "copy.log")
            else:
                channel.write_record(record())
        assert raised.value is (first if sync_control else close_control)
        result = raised.value.write_result
        assert result.appended and not result.durable
        assert result.sync_error and result.close_error
        assert result.failure_stage is WriteFailureStage.FILE_SYNC
        assert channel.disabled and close_attempts == 1
    finally:
        channel.close()


def test_rotation_always_retains_formal_names_for_kept_content(tmp_path, monkeypatch):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=1, file_count=4)
    for name in ("oldest", "older", "newer", "current"):
        assert channel.write_record(record(name)).appended
    original_rename = os.rename
    missing_at_rename = []
    protected = {"older", "newer", "current"}

    def rename(source, target, *args, **kwargs):
        value = original_rename(source, target, *args, **kwargs)
        available = {message for group in formal_contents(path).values() for message in group}
        if protected - available:
            missing_at_rename.append(protected - available)
        return value

    monkeypatch.setattr(os, "rename", rename)
    try:
        assert channel.write_record(record("later", LogLevel.INFO)).appended
        assert missing_at_rename == []
    finally:
        channel.close()


@pytest.mark.parametrize("via_copy", [False, True])
@pytest.mark.parametrize("acquire_control", [False, True])
def test_lock_failure_then_close_control_keeps_original_failure_and_facts(tmp_path, monkeypatch, via_copy, acquire_control):
    import concurrent_log_handler
    import portalocker.portalocker as portalocker_api

    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original_open = concurrent_log_handler._open
    cleanup_control = asyncio.CancelledError("lock cleanup control")
    failure = asyncio.CancelledError("lock acquire control") if acquire_control else OSError("lock denied")
    original = failure if acquire_control else cleanup_control

    class Stream:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def close(self):
            self.wrapped.close()
            raise cleanup_control

    monkeypatch.setattr(concurrent_log_handler, "_open",
                        lambda *args, **kwargs: Stream(original_open(*args, **kwargs)))
    monkeypatch.setattr(portalocker_api, "LOCKER",
                        lambda fd, operation: (_ for _ in ()).throw(failure))
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            if via_copy:
                channel.copy_with_write(record(), tmp_path / "copy.log")
            else:
                channel.write_record(record())
        assert raised.value is original
        result = original.write_result
        assert not result.appended and not result.durable
        assert result.failure_stage is WriteFailureStage.LOCK_ACQUIRE
        assert result.coordination_error and result.close_error
        if not acquire_control:
            assert original.__context__ is not None
        assert channel.disabled and not path.exists()
        if via_copy:
            later, copy_error = channel.copy_with_write(record("later"), tmp_path / "later.log")
            assert copy_error is None and later.copy_handles_closed
        else:
            later = channel.write_record(record("later"))
        assert not later.appended and not later.durable
    finally:
        channel.close()


@pytest.mark.parametrize("via_copy", [False, True])
@pytest.mark.parametrize("control", [False, True])
def test_format_failure_uses_common_result_exit_and_keeps_channel_available(tmp_path, monkeypatch, via_copy, control):
    import logging

    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original = asyncio.CancelledError("format control") if control else ValueError("format denied")

    def fail_format(self, entry):
        raise original

    try:
        with monkeypatch.context() as patch:
            patch.setattr(logging.Formatter, "format", fail_format)
            if control:
                with pytest.raises(asyncio.CancelledError) as raised:
                    if via_copy:
                        channel.copy_with_write(record(), tmp_path / "copy.log")
                    else:
                        channel.write_record(record())
                assert raised.value is original
                result = original.write_result
            elif via_copy:
                result, copy_error = channel.copy_with_write(record(), tmp_path / "copy.log")
                assert copy_error is None
            else:
                result = channel.write_record(record())
        assert not result.appended and not result.durable
        assert result.failure_stage is WriteFailureStage.FORMAT
        assert not channel.disabled and not path.exists()
        assert channel.write_record(record("later")).durable
    finally:
        channel.close()


def test_copy_control_and_unprintable_close_error_keep_durable_source_facts(tmp_path, monkeypatch):
    import builtins
    import shutil

    class UnprintableError(OSError):
        def __str__(self):
            raise ValueError("cannot format close error")

    path, destination = tmp_path / "camctl.log", tmp_path / "copy.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original_open = builtins.open
    original = asyncio.CancelledError("copy canceled")

    class Target:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def close(self):
            self.wrapped.close()
            raise UnprintableError()

    def open_file(name, *args, **kwargs):
        value = original_open(name, *args, **kwargs)
        return Target(value) if name == destination else value

    monkeypatch.setattr(builtins, "open", open_file)
    monkeypatch.setattr(shutil, "copyfileobj", lambda *args: (_ for _ in ()).throw(original))
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            channel.copy_with_write(record(), destination)
        assert raised.value is original
        result = original.write_result
        assert result.appended and result.durable
        assert result.copy_target_close_error and result.copy_handles_closed is False
        assert not channel.disabled and messages(path) == ["trigger"]
    finally:
        channel.close()


def test_unprintable_copy_oserror_returns_copy_failure_with_real_source_facts(tmp_path, monkeypatch):
    import shutil

    class UnprintableError(OSError):
        def __str__(self):
            raise ValueError("cannot format copy error")

    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    monkeypatch.setattr(shutil, "copyfileobj", lambda *args: (_ for _ in ()).throw(UnprintableError()))
    try:
        result, copy_error = channel.copy_with_write(record(), tmp_path / "copy.log")
        assert result.appended and result.durable
        assert copy_error and result.copy_handles_closed
        assert not channel.disabled
    finally:
        channel.close()


@pytest.mark.parametrize("via_copy,reject_note", [(False, False), (True, True)])
def test_result_setter_or_note_failure_never_replaces_original_control(tmp_path, monkeypatch, via_copy, reject_note):
    class UnattachableControl(asyncio.CancelledError):
        @property
        def write_result(self):
            return None

        @write_result.setter
        def write_result(self, value):
            raise RuntimeError("write result setter denied")

        def add_note(self, note):
            if reject_note:
                raise RuntimeError("note denied")
            return super().add_note(note)

    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original = UnattachableControl("original sync control")
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(original))
    try:
        with pytest.raises(UnattachableControl) as raised:
            if via_copy:
                channel.copy_with_write(record(), tmp_path / "copy.log")
            else:
                channel.write_record(record())
        assert raised.value is original
        assert messages(path) == ["trigger"] and channel.disabled
        if not reject_note:
            assert original.__notes__
    finally:
        channel.close()


@pytest.mark.parametrize("via_copy", [False, True])
@pytest.mark.parametrize("fail_actual_directory", [False, True])
def test_symlink_target_and_formal_name_directories_are_both_synced(tmp_path, monkeypatch, via_copy, fail_actual_directory):
    configured, actual = tmp_path / "configured", tmp_path / "actual"
    configured.mkdir()
    actual.mkdir()
    path, target = configured / "camctl.log", actual / "actual.log"
    path.symlink_to(target)
    assert not target.exists()
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original = os.fsync
    synced = []
    actual_identity = actual.stat().st_dev, actual.stat().st_ino

    def fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            identity = info.st_dev, info.st_ino
            synced.append(identity)
            if fail_actual_directory and identity == actual_identity:
                raise OSError("actual directory sync denied")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    try:
        if via_copy:
            destination = tmp_path / "copy.log"
            result, copy_error = channel.copy_with_write(record(), destination)
            assert copy_error is None
            assert destination.exists() is (not fail_actual_directory)
        else:
            result = channel.write_record(record())
        assert result.appended and result.durable is (not fail_actual_directory)
        assert messages(target) == ["trigger"]
        if fail_actual_directory:
            assert result.failure_stage is WriteFailureStage.DIRECTORY_SYNC
            assert result.sync_error and channel.disabled
            assert synced.count(actual_identity) == 1
            before = list(synced)
            assert not channel.write_record(record("later")).appended
            assert synced == before
        else:
            expected = {(info.st_dev, info.st_ino) for info in
                        (directory.stat() for directory in [actual, configured, *configured.parents])}
            assert set(synced) == expected
            assert len(synced) == len(expected)
            assert not channel.disabled
    finally:
        channel.close()


def test_actual_path_resolution_failure_preserves_append_and_reports_directory_sync(tmp_path, monkeypatch):
    path = tmp_path / "camctl.log"
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    original = Path.resolve

    def resolve(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError("actual path unresolved")
        return original(candidate, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "resolve", resolve)
            result = channel.write_record(record())
        assert result.appended and not result.durable
        assert result.failure_stage is WriteFailureStage.DIRECTORY_SYNC
        assert result.sync_error and channel.disabled
        assert messages(path) == ["trigger"]
    finally:
        channel.close()


@pytest.mark.parametrize("scenario", ["copy_error", "close_errors", "copy_control", "close_control", "close_group", "file_control"])
def test_copy_selection_and_independent_operation_control_use_separate_responsibility(tmp_path, monkeypatch, scenario):
    import builtins
    import fcntl
    import portalocker.portalocker as portalocker_api
    import shutil

    path, destination = tmp_path / "camctl.log", tmp_path / "copy.log"
    path.touch()
    channel = FileChannel(path, max_bytes=4096, file_count=3)
    operation_control = asyncio.CancelledError("independent unlock control")
    copy_control = asyncio.CancelledError("earlier copy control")
    file_control = asyncio.CancelledError("earlier file sync control")
    copy_error = RuntimeError("copy operation failed")
    target_error = RuntimeError("target close failed") if scenario == "close_errors" else OSError("target close failed")
    source_error = RuntimeError("source close failed") if scenario == "close_group" else asyncio.CancelledError("source close control")
    original_open, original_copy, original_flock, original_sync = builtins.open, shutil.copyfileobj, fcntl.flock, os.fsync
    copy_started, unlock_interrupted = [], False

    class Stream:
        def __init__(self, wrapped, error):
            self.wrapped, self.error = wrapped, error

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def close(self):
            self.wrapped.close()
            raise self.error

    def open_file(name, mode="r", *args, **kwargs):
        value = original_open(name, mode, *args, **kwargs)
        if scenario in ("close_errors", "close_control", "close_group"):
            if name == path and mode == "rb":
                return Stream(value, source_error)
            if name == destination and mode == "wb":
                return Stream(value, target_error)
        return value

    def copy(source, target):
        copy_started.append(True)
        if scenario == "copy_error":
            raise copy_error
        if scenario == "copy_control":
            raise copy_control
        return original_copy(source, target)

    def flock(fd, flags):
        nonlocal unlock_interrupted
        value = original_flock(fd, flags)
        if flags == fcntl.LOCK_UN and not unlock_interrupted:
            unlock_interrupted = True
            raise operation_control
        return value

    def fsync(fd):
        if scenario == "file_control" and stat.S_ISREG(os.fstat(fd).st_mode):
            raise file_control
        return original_sync(fd)

    monkeypatch.setattr(builtins, "open", open_file)
    monkeypatch.setattr(shutil, "copyfileobj", copy)
    monkeypatch.setattr(portalocker_api, "LOCKER", flock)
    monkeypatch.setattr(os, "fsync", fsync)
    try:
        with pytest.raises(BaseException) as raised:
            channel.copy_with_write(record(), destination)
        expected = (copy_control if scenario == "copy_control" else
                    source_error if scenario == "close_control" else
                    file_control if scenario == "file_control" else operation_control)
        assert raised.value is expected
        result = expected.write_result
        assert result.appended and result.durable is (scenario != "file_control")
        assert result.coordination_error and channel.disabled
        if scenario in ("copy_control", "close_control"):
            assert operation_control.write_result is result
            assert any("independent unlock control" in note for note in expected.__notes__)
        elif scenario == "file_control":
            assert not copy_started and not destination.exists()
            assert result.failure_stage is WriteFailureStage.FILE_SYNC
        else:
            context = expected.__context__
            if scenario == "copy_error":
                assert context is copy_error
            elif scenario == "close_errors":
                assert context is target_error
                assert any("source close control" in note for note in target_error.__notes__)
            else:
                assert isinstance(context, ExceptionGroup)
                assert context.exceptions == (target_error, source_error)
        if scenario == "close_control":
            assert source_error.__cause__ is target_error
    finally:
        channel.close()
