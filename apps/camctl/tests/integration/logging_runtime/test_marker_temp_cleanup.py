"""真实文件和线程中的标记临时文件所有权与取消收场。"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from camctl.logging_runtime.copies import (
    MARKER_NAME,
    MarkerStage,
    MarkerStore,
    SaveOutcome,
    is_marker_temp_name,
)

pytestmark = pytest.mark.asyncio


def _logs_dir(tmp_path: Path) -> Path:
    logs = tmp_path / "staging" / "logs"
    logs.mkdir(parents=True)
    return logs


def _seed_store(logs: Path) -> tuple[MarkerStore, Path, bytes]:
    store = MarkerStore(logs)
    saved = store.create("r1", "log-copy-r1.log")
    assert saved.outcome is SaveOutcome.SAVED
    marker = logs / MARKER_NAME
    return store, marker, marker.read_bytes()


async def test_real_partial_write_is_removed_after_handle_close(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store, marker, original_marker = _seed_store(logs)
    real_open, real_write, real_close, real_unlink = os.open, os.write, os.close, os.unlink
    descriptors: dict[int, Path] = {}
    write_paths: list[Path] = []
    closed: list[Path] = []
    removed_bytes: list[bytes] = []
    written_prefixes: list[bytes] = []

    def open_temp(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        candidate = Path(os.fsdecode(path))
        if candidate.parent == logs and is_marker_temp_name(candidate.name):
            descriptors[fd] = candidate
            write_paths.append(candidate)
        return fd

    def partial_write(fd, data):
        if fd in descriptors:
            if not removed_bytes and os.fstat(fd).st_size == 0:
                written = real_write(fd, data[:7])
                written_prefixes.append(bytes(data[:written]))
                return written
            raise OSError("controlled partial marker write failure")
        return real_write(fd, data)

    def close_temp(fd):
        path = descriptors.pop(fd, None)
        result = real_close(fd)
        if path is not None:
            closed.append(path)
        return result

    def remove_temp(path, *args, **kwargs):
        if write_paths and Path(os.fsdecode(path)) == write_paths[0]:
            assert closed == write_paths
            removed_bytes.append(Path(os.fsdecode(path)).read_bytes())
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_temp)
    monkeypatch.setattr(os, "write", partial_write)
    monkeypatch.setattr(os, "close", close_temp)
    monkeypatch.setattr(os, "unlink", remove_temp)
    result = await asyncio.to_thread(
        store.update_stage,
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.FAILED_NOT_CREATED
    assert "controlled partial marker write failure" in (result.error or "")
    assert write_paths[0].name.startswith(MARKER_NAME + ".")
    assert len(written_prefixes) == 1 and len(written_prefixes[0]) == 7
    assert removed_bytes == written_prefixes
    assert write_paths[0].exists() is False
    assert marker.read_bytes() == original_marker
    assert not any(is_marker_temp_name(path.name) for path in logs.iterdir())


async def test_real_replace_failure_cleans_temp_and_preserves_marker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store, marker, original_marker = _seed_store(logs)
    replace_paths: list[Path] = []

    real_replace = os.replace

    def fail_before_placement(source, target, *args, **kwargs):
        if Path(os.fsdecode(target)) == marker and Path(os.fsdecode(source)).parent == logs:
            replace_paths.append(Path(os.fsdecode(source)))
            raise OSError("controlled replace failure")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_before_placement)
    result = await asyncio.to_thread(
        store.update_stage,
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.UNKNOWN
    assert "controlled replace failure" in (result.error or "")
    assert replace_paths[0].exists() is False
    assert marker.read_bytes() == original_marker
    assert not any(is_marker_temp_name(path.name) for path in logs.iterdir())


async def test_real_unconfirmed_temp_close_preserves_open_owned_temp(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store, marker, original_marker = _seed_store(logs)
    real_open = os.open
    real_close = os.close
    temp_fds: set[int] = set()
    close_calls: list[int] = []
    temp_paths: list[Path] = []

    def track_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        candidate = Path(os.fsdecode(path))
        if candidate.parent == logs and is_marker_temp_name(candidate.name):
            temp_fds.add(fd)
            temp_paths.append(candidate)
        return fd

    def fail_temp_close(fd: int) -> None:
        if fd in temp_fds:
            close_calls.append(fd)
            raise OSError("real temp close result unconfirmed")
        real_close(fd)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "close", fail_temp_close)
    try:
        result = await asyncio.to_thread(
            store.update_stage,
            MarkerStage.COPIED,
            expected_round_id="r1",
            expected_copy_name="log-copy-r1.log",
        )

        assert result.outcome is SaveOutcome.FAILED_NOT_CREATED
        assert "real temp close result unconfirmed" in (result.error or "")
        assert getattr(result, "temp_name", None) == temp_paths[0].name
        assert getattr(result, "temp_cleanup", "not-added") is None
        assert temp_paths[0].is_file()
        assert marker.read_bytes() == original_marker
        assert close_calls == list(temp_fds)
    finally:
        # The injected close deliberately leaves the real descriptor open.
        # Reclaim this test resource only after asserting production facts.
        for fd in tuple(temp_fds):
            temp_fds.remove(fd)
            real_close(fd)
        for path in temp_paths:
            path.unlink(missing_ok=True)


async def test_real_runtime_write_error_keeps_identity_when_close_oserror(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store, marker, original_marker = _seed_store(logs)
    primary = RuntimeError("actual marker write program failure")
    close_error = OSError("actual temp close result unknown")
    real_open = os.open
    real_write = os.write
    real_close = os.close
    real_fstat = os.fstat
    descriptor: int | None = None
    temp_path: Path | None = None
    close_calls: list[int] = []
    unlink_calls: list[Path] = []
    real_unlink = os.unlink

    def tracked_open(path, flags, *args, **kwargs):
        nonlocal descriptor, temp_path
        fd = real_open(path, flags, *args, **kwargs)
        candidate = Path(os.fsdecode(path))
        if candidate.parent == logs and is_marker_temp_name(candidate.name):
            descriptor = fd
            temp_path = candidate
        return fd

    def fail_write(fd: int, data: memoryview) -> int:
        if fd == descriptor:
            raise primary
        return real_write(fd, data)

    def fail_temp_close(fd: int) -> None:
        if fd == descriptor:
            close_calls.append(fd)
            raise close_error
        real_close(fd)

    def track_unlink(path, *args, **kwargs):
        if temp_path is not None and Path(os.fsdecode(path)) == temp_path:
            unlink_calls.append(Path(os.fsdecode(path)))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "write", fail_write)
    monkeypatch.setattr(os, "close", fail_temp_close)
    monkeypatch.setattr(os, "unlink", track_unlink)

    try:
        with pytest.raises(RuntimeError) as caught:
            await asyncio.to_thread(
                store.update_stage,
                MarkerStage.COPIED,
                expected_round_id="r1",
                expected_copy_name="log-copy-r1.log",
            )

        assert caught.value is primary
        assert descriptor is not None and temp_path is not None
        assert temp_path.is_file()
        assert marker.read_bytes() == original_marker
        real_fstat(descriptor)
        assert close_calls == [descriptor]
        assert unlink_calls == []
        notes = "\n".join(getattr(primary, "__notes__", ()))
        assert str(close_error) in notes
        assert temp_path.name in notes
        assert "temp_cleanup=not_attempted" in notes
    finally:
        if descriptor is not None:
            fd_to_close = descriptor
            descriptor = None
            real_close(fd_to_close)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _is_directory(fd: int, directory: Path) -> bool:
    opened, named = os.fstat(fd), directory.stat()
    return (stat.S_ISDIR(opened.st_mode)
            and (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino))


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不提供目录同步")
async def test_real_directory_sync_failure_keeps_placed_formal_marker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store, marker, original_marker = _seed_store(logs)
    real_fsync = os.fsync
    directory_syncs: list[int] = []
    write_paths: list[Path] = []
    real_replace = os.replace

    def record_placement(source, target, *args, **kwargs):
        if Path(os.fsdecode(target)) == marker:
            write_paths.append(Path(os.fsdecode(source)))
        return real_replace(source, target, *args, **kwargs)

    def fail_directory_sync(fd: int) -> None:
        if _is_directory(fd, logs):
            directory_syncs.append(fd)
            raise OSError("real directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    monkeypatch.setattr(os, "replace", record_placement)
    result = await asyncio.to_thread(
        store.update_stage,
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.PLACED_UNSYNCED
    assert "real directory fsync failed" in (result.error or "")
    assert getattr(result, "temp_name", None) == write_paths[0].name
    cleanup = getattr(result, "temp_cleanup", None)
    assert cleanup is not None
    assert cleanup.outcome.value == "not_present"
    assert marker.read_bytes() != original_marker
    assert store.check().state.stage is MarkerStage.COPIED
    assert len(directory_syncs) == 1
    assert not any(is_marker_temp_name(path.name) for path in logs.iterdir())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不提供目录同步")
async def test_directory_sync_error_survives_close_oserror(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store, marker, original_marker = _seed_store(logs)
    real_fsync = os.fsync
    real_close = os.close

    def fail_directory_sync(fd: int) -> None:
        if _is_directory(fd, logs):
            raise OSError("primary directory fsync error")
        real_fsync(fd)

    def close_directory_then_report_error(fd: int) -> None:
        if _is_directory(fd, logs):
            real_close(fd)
            raise OSError("secondary directory close error")
        real_close(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    monkeypatch.setattr(os, "close", close_directory_then_report_error)
    result = await asyncio.to_thread(
        store.update_stage,
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.PLACED_UNSYNCED
    assert "primary directory fsync error" in (result.error or "")
    assert "secondary directory close error" in (result.error or "")
    assert marker.read_bytes() != original_marker
    assert store.check().state.stage is MarkerStage.COPIED
    assert not any(is_marker_temp_name(path.name) for path in logs.iterdir())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不提供目录同步")
async def test_directory_sync_success_survives_close_oserror_for_save_and_delete(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store = MarkerStore(logs)
    real_open, real_fsync, real_close = os.open, os.fsync, os.close
    directory_fds: set[int] = set()
    synced_fds: set[int] = set()
    close_followed_sync: list[bool] = []

    def open_directory(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if Path(os.fsdecode(path)) == logs:
            directory_fds.add(fd)
        return fd

    def sync_directory(fd):
        result = real_fsync(fd)
        if fd in directory_fds:
            synced_fds.add(fd)
        return result

    def close_directory_then_report_error(fd: int) -> None:
        tracked = fd in directory_fds
        if tracked:
            directory_fds.remove(fd)
            synced = fd in synced_fds
            synced_fds.discard(fd)
        real_close(fd)
        if tracked:
            close_followed_sync.append(synced)
            raise OSError("directory close result unconfirmed")

    monkeypatch.setattr(os, "open", open_directory)
    monkeypatch.setattr(os, "fsync", sync_directory)
    monkeypatch.setattr(os, "close", close_directory_then_report_error)
    saved = await asyncio.to_thread(store.create, "r1", "log-copy-r1.log")

    assert saved.outcome is SaveOutcome.SAVED
    assert "directory close result unconfirmed" in (saved.error or "")
    assert getattr(saved, "temp_name", None) is not None
    save_cleanup = getattr(saved, "temp_cleanup", None)
    assert save_cleanup is not None
    assert save_cleanup.outcome.value == "not_present"
    assert store.check().state.round_id == "r1"

    deleted = await asyncio.to_thread(store.delete)

    assert deleted.outcome.value == "deleted"
    assert "directory close result unconfirmed" in (deleted.error or "")
    assert store.check().presence.value == "absent"
    assert close_followed_sync == [True, True]
    assert directory_fds == synced_fds == set()


async def test_cancelled_wait_keeps_active_temp_until_save_owner_finishes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store, marker, original_marker = _seed_store(logs)
    started = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    write_paths: list[Path] = []
    closed: list[Path] = []
    removed_bytes: list[bytes] = []
    written_prefixes: list[bytes] = []
    results = []
    real_open, real_write, real_close, real_unlink = os.open, os.write, os.close, os.unlink
    descriptors: dict[int, Path] = {}

    def open_temp(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        candidate = Path(os.fsdecode(path))
        if candidate.parent == logs and is_marker_temp_name(candidate.name):
            descriptors[fd] = candidate
            write_paths.append(candidate)
        return fd

    def blocked_partial_write(fd, data):
        if fd not in descriptors:
            return real_write(fd, data)
        if os.fstat(fd).st_size == 0:
            written = real_write(fd, data[:7])
            written_prefixes.append(bytes(data[:written]))
            started.set()
            if not release.wait(timeout=10):
                raise RuntimeError("test did not release marker writer")
            return written
        raise OSError("controlled marker write failure")

    def close_temp(fd):
        path = descriptors.pop(fd, None)
        result = real_close(fd)
        if path is not None:
            closed.append(path)
        return result

    def remove_temp(path, *args, **kwargs):
        if write_paths and Path(os.fsdecode(path)) == write_paths[0]:
            assert closed == write_paths
            removed_bytes.append(Path(os.fsdecode(path)).read_bytes())
        return real_unlink(path, *args, **kwargs)

    def save_in_worker() -> None:
        try:
            results.append(store.update_stage(
                MarkerStage.COPIED,
                expected_round_id="r1",
                expected_copy_name="log-copy-r1.log",
            ))
        finally:
            worker_finished.set()

    monkeypatch.setattr(os, "open", open_temp)
    monkeypatch.setattr(os, "write", blocked_partial_write)
    monkeypatch.setattr(os, "close", close_temp)
    monkeypatch.setattr(os, "unlink", remove_temp)
    waiting = asyncio.create_task(asyncio.to_thread(save_in_worker))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        temp = write_paths[0]
        assert temp.exists() and temp.read_bytes() == written_prefixes[0]
        assert len(written_prefixes[0]) == 7
        assert closed == [] and removed_bytes == []
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert temp.exists()
    finally:
        release.set()
        assert await asyncio.to_thread(worker_finished.wait, 10)

    assert len(results) == 1 and results[0].outcome is SaveOutcome.FAILED_NOT_CREATED
    assert "controlled marker write failure" in (results[0].error or "")
    assert removed_bytes == written_prefixes
    assert write_paths[0].exists() is False
    assert marker.read_bytes() == original_marker
    assert not any(is_marker_temp_name(path.name) for path in logs.iterdir())
