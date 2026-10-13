"""真实文件和线程中的标记临时文件所有权与取消收场。"""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from pathlib import Path

import pytest

from camctl.logging_runtime import copies
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
    write_paths: list[Path] = []

    def write_partial_then_fail(path: Path, data: bytes) -> None:
        write_paths.append(path)
        with path.open("wb") as stream:
            stream.write(data[:7])
        raise OSError("controlled partial marker write failure")

    monkeypatch.setattr(copies, "_write_synced", write_partial_then_fail)
    result = await asyncio.to_thread(
        store.update_stage,
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.FAILED_NOT_CREATED
    assert "controlled partial marker write failure" in (result.error or "")
    assert write_paths[0].name.startswith(MARKER_NAME + ".")
    assert write_paths[0].exists() is False
    assert marker.read_bytes() == original_marker
    assert not any(is_marker_temp_name(path.name) for path in logs.iterdir())


async def test_real_replace_failure_cleans_temp_and_preserves_marker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store, marker, original_marker = _seed_store(logs)
    replace_paths: list[Path] = []

    def fail_before_placement(source: Path, _target: Path) -> None:
        replace_paths.append(source)
        raise OSError("controlled replace failure")

    monkeypatch.setattr(copies, "_replace", fail_before_placement)
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

    def track_open(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        candidate = Path(path)
        if candidate.name.startswith(MARKER_NAME + "."):
            temp_fds.add(fd)
            temp_paths.append(candidate)
        return fd

    def fail_temp_close(fd: int) -> None:
        if fd in temp_fds:
            close_calls.append(fd)
            raise OSError("real temp close result unconfirmed")
        real_close(fd)

    monkeypatch.setattr(copies.os, "open", track_open)
    monkeypatch.setattr(copies.os, "close", fail_temp_close)
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
    real_unlink = copies._unlink

    def tracked_open(path, flags, mode=0o777):
        nonlocal descriptor, temp_path
        fd = real_open(path, flags, mode)
        candidate = Path(path)
        if candidate.name.startswith(MARKER_NAME + "."):
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

    def track_unlink(path: Path) -> None:
        unlink_calls.append(Path(path))
        real_unlink(path)

    monkeypatch.setattr(copies.os, "open", tracked_open)
    monkeypatch.setattr(copies.os, "write", fail_write)
    monkeypatch.setattr(copies.os, "close", fail_temp_close)
    monkeypatch.setattr(copies, "_unlink", track_unlink)

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
            real_close(descriptor)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


async def test_real_directory_sync_failure_keeps_placed_formal_marker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store, marker, original_marker = _seed_store(logs)
    real_fsync = os.fsync
    directory_syncs: list[int] = []
    write_paths: list[Path] = []
    real_write = copies._write_synced

    def record_write(path: Path, data: bytes) -> None:
        write_paths.append(path)
        real_write(path, data)

    def fail_directory_sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs.append(fd)
            raise OSError("real directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(copies.os, "fsync", fail_directory_sync)
    monkeypatch.setattr(copies, "_write_synced", record_write)
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


async def test_directory_sync_error_survives_close_oserror(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store, marker, original_marker = _seed_store(logs)
    real_fsync = os.fsync
    real_close = os.close

    def fail_directory_sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("primary directory fsync error")
        real_fsync(fd)

    def close_directory_then_report_error(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            real_close(fd)
            raise OSError("secondary directory close error")
        real_close(fd)

    monkeypatch.setattr(copies.os, "fsync", fail_directory_sync)
    monkeypatch.setattr(copies.os, "close", close_directory_then_report_error)
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


async def test_directory_sync_success_survives_close_oserror_for_save_and_delete(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store = MarkerStore(logs)
    real_close = os.close
    directory_fds: list[int] = []

    def close_directory_then_report_error(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fds.append(fd)
            real_close(fd)
            raise OSError("directory close result unconfirmed")
        real_close(fd)

    monkeypatch.setattr(copies.os, "close", close_directory_then_report_error)
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
    assert len(directory_fds) == 2


async def test_cancelled_wait_keeps_active_temp_until_save_owner_finishes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    store, marker, original_marker = _seed_store(logs)
    started = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    write_paths: list[Path] = []

    def blocked_partial_write(path: Path, data: bytes) -> None:
        write_paths.append(path)
        with path.open("wb") as stream:
            stream.write(data[:7])
            started.set()
            if not release.wait(timeout=10):
                raise RuntimeError("test did not release marker writer")
        raise OSError("controlled write failure after close")

    def save_in_worker() -> None:
        try:
            store.update_stage(
                MarkerStage.COPIED,
                expected_round_id="r1",
                expected_copy_name="log-copy-r1.log",
            )
        finally:
            worker_finished.set()

    monkeypatch.setattr(copies, "_write_synced", blocked_partial_write)
    waiting = asyncio.create_task(asyncio.to_thread(save_in_worker))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        temp = write_paths[0]
        assert temp.exists()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert temp.exists()
    finally:
        release.set()

    assert await asyncio.to_thread(worker_finished.wait, 5)
    assert write_paths[0].exists() is False
    assert marker.read_bytes() == original_marker
    assert not any(is_marker_temp_name(path.name) for path in logs.iterdir())
