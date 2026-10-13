"""标记保存只清理本次路径，并保留保存与清理两侧的错误事实。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from camctl.logging_runtime import copies
from camctl.logging_runtime.copies import (
    MARKER_NAME,
    CopyOutcome,
    CopyReceipt,
    DeleteOutcome,
    MarkerDelete,
    MarkerStage,
    MarkerSave,
    MarkerStore,
    SaveOutcome,
    is_marker_temp_name,
)
from camctl.session.service import (
    _copy_receipt_diagnostic,
    _failure_copy_exception,
)


_LOGS = Path("memory-staging") / "logs"
_MARKER = _LOGS / MARKER_NAME
_OLD_MARKER = (
    b'{"round_id":"r1","copy_name":"log-copy-r1.log",'
    b'"stage":"triggered"}'
)
_REAL_WRITE_SYNCED = copies._write_synced
_REAL_FSYNC_DIRECTORY = copies._fsync_directory


class MemoryMarkerFiles:
    """只替换文件系统边界；MarkerStore 和保存结果保持真实。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.files: dict[Path, bytes] = {}
        self.calls: list[tuple] = []
        self.write_paths: list[Path] = []
        self.unlink_paths: list[Path] = []
        self.write_hook = None
        self.replace_hook = None
        self.unlink_hook = None
        self.directory_sync_error: BaseException | None = None
        self._original_mkdir = Path.mkdir

        monkeypatch.setattr(copies, "_exists", self.exists)
        monkeypatch.setattr(copies, "_read_bytes", self.read_bytes)
        monkeypatch.setattr(copies, "_write_synced", self.write_synced)
        monkeypatch.setattr(copies, "_replace", self.replace)
        monkeypatch.setattr(copies, "_fsync_directory", self.fsync_directory)
        monkeypatch.setattr(copies, "_unlink", self.unlink)
        monkeypatch.setattr(
            Path, "mkdir",
            lambda path, *args, **kwargs: self.mkdir(path, *args, **kwargs),
        )

    def mkdir(self, path: Path, *args, **kwargs) -> None:
        if Path(path) != _LOGS:
            self._original_mkdir(path, *args, **kwargs)
            return
        self.calls.append(("mkdir", Path(path)))

    def exists(self, path: Path) -> bool:
        self.calls.append(("exists", Path(path)))
        return Path(path) in self.files

    def read_bytes(self, path: Path) -> bytes:
        self.calls.append(("read", Path(path)))
        return self.files[Path(path)]

    def write_synced(self, path: Path, data: bytes) -> None:
        path = Path(path)
        self.calls.append(("write", path))
        self.write_paths.append(path)
        if self.write_hook is not None:
            self.write_hook(path, bytes(data))
        self.files[path] = bytes(data)

    def replace(self, source: Path, target: Path) -> None:
        source, target = Path(source), Path(target)
        self.calls.append(("replace", source, target))
        if self.replace_hook is not None:
            self.replace_hook(source, target)
            return
        self.files[target] = self.files.pop(source)

    def fsync_directory(self, path: Path) -> None:
        self.calls.append(("fsync_directory", Path(path)))
        if self.directory_sync_error is not None:
            raise self.directory_sync_error

    def unlink(self, path: Path) -> None:
        path = Path(path)
        self.calls.append(("unlink", path))
        self.unlink_paths.append(path)
        if self.unlink_hook is not None:
            self.unlink_hook(path)
            return
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]

    def temp_paths(self) -> set[Path]:
        return {path for path in self.files if is_marker_temp_name(path.name)}


def _store(monkeypatch: pytest.MonkeyPatch) -> tuple[MarkerStore, MemoryMarkerFiles]:
    files = MemoryMarkerFiles(monkeypatch)
    return MarkerStore(_LOGS), files


def _seed_marker(files: MemoryMarkerFiles) -> None:
    files.files[_MARKER] = _OLD_MARKER


def _foreign_temp(files: MemoryMarkerFiles) -> Path:
    path = _LOGS / "failure-marker.json.foreign.tmp"
    files.files[path] = b"other save owns this"
    return path


def _assert_temp_facts(
    save: MarkerSave, name: str, outcome: DeleteOutcome | None,
    error: str | None = None,
) -> None:
    assert getattr(save, "temp_name", None) == name
    cleanup = getattr(save, "temp_cleanup", None)
    if outcome is None:
        assert cleanup is None
        return
    assert cleanup is not None
    assert cleanup.outcome is outcome
    if error is None:
        assert cleanup.error is None
    else:
        assert error in cleanup.error


def test_replace_success_records_moved_temp_without_extra_unlink(monkeypatch):
    store, files = _store(monkeypatch)

    result = store.create("r1", "log-copy-r1.log")

    assert result.outcome is SaveOutcome.SAVED
    _assert_temp_facts(result, files.write_paths[0].name,
                       DeleteOutcome.NOT_PRESENT)
    assert files.unlink_paths == []
    assert _MARKER in files.files


@pytest.mark.parametrize("entrypoint", ["create", "update_stage"])
def test_partial_write_failure_removes_only_its_temp(monkeypatch, entrypoint):
    store, files = _store(monkeypatch)
    _seed_marker(files)
    foreign = _foreign_temp(files)
    primary = OSError("injected partial write failure")

    def write_partial(path: Path, data: bytes) -> None:
        files.files[path] = data[:5]
        raise primary

    files.write_hook = write_partial
    if entrypoint == "create":
        result = store.create("r1", "log-copy-r1.log")
    else:
        result = store.update_stage(
            MarkerStage.COPIED,
            expected_round_id="r1",
            expected_copy_name="log-copy-r1.log",
        )

    assert result.outcome is SaveOutcome.FAILED_NOT_CREATED
    assert "injected partial write failure" in (result.error or "")
    _assert_temp_facts(result, files.write_paths[0].name,
                       DeleteOutcome.DELETED)
    assert files.files[_MARKER] == _OLD_MARKER
    assert files.temp_paths() == {foreign}
    assert files.files[foreign] == b"other save owns this"
    assert files.unlink_paths == [files.write_paths[0]]


def test_replace_failure_cleans_temp_and_preserves_formal_marker(monkeypatch):
    store, files = _store(monkeypatch)
    _seed_marker(files)
    foreign = _foreign_temp(files)

    def fail_before_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace did not place marker")

    files.replace_hook = fail_before_replace
    result = store.update_stage(
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.UNKNOWN
    assert "replace did not place marker" in (result.error or "")
    _assert_temp_facts(result, files.write_paths[0].name,
                       DeleteOutcome.DELETED)
    assert files.files[_MARKER] == _OLD_MARKER
    assert files.temp_paths() == {foreign}
    assert files.files[foreign] == b"other save owns this"
    assert files.unlink_paths == [files.write_paths[0]]


def test_replace_result_unknown_keeps_possible_formal_placement(monkeypatch):
    store, files = _store(monkeypatch)
    _seed_marker(files)
    foreign = _foreign_temp(files)

    def place_then_report_unknown(source: Path, target: Path) -> None:
        files.files[target] = files.files.pop(source)
        raise OSError("replace result unknown after placement")

    files.replace_hook = place_then_report_unknown
    result = store.update_stage(
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.UNKNOWN
    assert "replace result unknown after placement" in (result.error or "")
    _assert_temp_facts(result, files.write_paths[0].name,
                       DeleteOutcome.NOT_PRESENT)
    assert files.files[_MARKER] != _OLD_MARKER
    assert files.temp_paths() == {foreign}
    assert files.files[foreign] == b"other save owns this"


def test_directory_sync_failure_keeps_placed_marker(monkeypatch):
    store, files = _store(monkeypatch)
    _seed_marker(files)
    files.directory_sync_error = OSError("directory sync failed")

    result = store.update_stage(
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.PLACED_UNSYNCED
    assert "directory sync failed" in (result.error or "")
    _assert_temp_facts(result, files.write_paths[0].name,
                       DeleteOutcome.NOT_PRESENT)
    assert files.files[_MARKER] != _OLD_MARKER
    assert files.temp_paths() == set()


def test_missing_temp_after_write_failure_is_not_a_cleanup_error(monkeypatch):
    store, files = _store(monkeypatch)
    _seed_marker(files)

    def write_then_disappear(path: Path, _data: bytes) -> None:
        files.files[path] = b"partial"
        del files.files[path]
        raise OSError("write failed after temp disappeared")

    files.write_hook = write_then_disappear
    result = store.update_stage(
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.FAILED_NOT_CREATED
    _assert_temp_facts(result, files.write_paths[0].name,
                       DeleteOutcome.NOT_PRESENT)
    assert files.temp_paths() == set()
    assert files.unlink_paths == [files.write_paths[0]]


def test_cleanup_oserror_preserves_save_error_and_temp_ownership(monkeypatch):
    store, files = _store(monkeypatch)
    _seed_marker(files)

    def write_partial(path: Path, _data: bytes) -> None:
        files.files[path] = b"partial"
        raise OSError("primary marker write error")

    def fail_cleanup(path: Path) -> None:
        raise OSError("secondary temp unlink error")

    files.write_hook = write_partial
    files.unlink_hook = fail_cleanup
    result = store.update_stage(
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.FAILED_NOT_CREATED
    assert "primary marker write error" in (result.error or "")
    _assert_temp_facts(result, files.write_paths[0].name,
                       DeleteOutcome.FAILED,
                       "temp_cleanup_failed: OSError: secondary temp unlink error")
    assert files.temp_paths() == {files.write_paths[0]}
    assert files.unlink_paths == [files.write_paths[0]]


def test_unlink_interrupted_is_attempted_once_for_owned_temp(monkeypatch):
    store, files = _store(monkeypatch)
    _seed_marker(files)

    def write_partial(path: Path, _data: bytes) -> None:
        files.files[path] = b"partial"
        raise OSError("primary marker write error")

    def interrupt_cleanup(_path: Path) -> None:
        raise InterruptedError("unlink interrupted")

    files.write_hook = write_partial
    files.unlink_hook = interrupt_cleanup
    result = store.update_stage(
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.FAILED_NOT_CREATED
    cleanup = getattr(result, "temp_cleanup", None)
    assert getattr(result, "temp_name", None) == files.write_paths[0].name
    assert cleanup is not None
    assert cleanup.outcome is DeleteOutcome.UNKNOWN
    assert "unlink interrupted" in cleanup.error
    assert files.temp_paths() == {files.write_paths[0]}
    assert files.unlink_paths == [files.write_paths[0]]


def test_unconfirmed_close_keeps_temp_without_retrying_close(monkeypatch):
    store, files = _store(monkeypatch)
    _seed_marker(files)
    fd_paths: dict[int, Path] = {}
    close_calls: list[int] = []

    def open_file(path: Path, _flags: int, _mode: int) -> int:
        fd_paths[71] = Path(path)
        files.files[Path(path)] = b""
        return 71

    def write_file(fd: int, data: memoryview) -> int:
        path = fd_paths[fd]
        files.files[path] += bytes(data)
        return len(data)

    def close_file(fd: int) -> None:
        close_calls.append(fd)
        raise OSError("close result unconfirmed")

    monkeypatch.setattr(copies, "_write_synced", _REAL_WRITE_SYNCED)
    monkeypatch.setattr(copies.os, "open", open_file)
    monkeypatch.setattr(copies.os, "write", write_file)
    monkeypatch.setattr(copies.os, "fsync", lambda _fd: None)
    monkeypatch.setattr(copies.os, "close", close_file)

    result = store.update_stage(
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    temp = fd_paths[71]
    assert result.outcome is SaveOutcome.FAILED_NOT_CREATED
    assert "close result unconfirmed" in (result.error or "")
    _assert_temp_facts(result, fd_paths[71].name, None)
    assert temp in files.files
    assert close_calls == [71]
    assert files.unlink_paths == []


def test_write_and_close_errors_are_both_preserved(monkeypatch):
    store, files = _store(monkeypatch)
    _seed_marker(files)
    fd_paths: dict[int, Path] = {}
    close_calls: list[int] = []

    def open_file(path: Path, _flags: int, _mode: int) -> int:
        fd_paths[72] = Path(path)
        files.files[Path(path)] = b""
        return 72

    def write_file(_fd: int, _data: memoryview) -> int:
        raise OSError("primary write syscall error")

    def close_file(fd: int) -> None:
        close_calls.append(fd)
        raise OSError("secondary close syscall error")

    monkeypatch.setattr(copies, "_write_synced", _REAL_WRITE_SYNCED)
    monkeypatch.setattr(copies.os, "open", open_file)
    monkeypatch.setattr(copies.os, "write", write_file)
    monkeypatch.setattr(copies.os, "fsync", lambda _fd: None)
    monkeypatch.setattr(copies.os, "close", close_file)

    result = store.update_stage(
        MarkerStage.COPIED,
        expected_round_id="r1",
        expected_copy_name="log-copy-r1.log",
    )

    assert result.outcome is SaveOutcome.FAILED_NOT_CREATED
    assert "primary write syscall error" in (result.error or "")
    assert "secondary close syscall error" in (result.error or "")
    _assert_temp_facts(result, fd_paths[72].name, None)
    assert fd_paths[72] in files.files
    assert close_calls == [72]
    assert files.unlink_paths == []


@pytest.mark.parametrize("primary_step", ["write", "fsync"])
def test_temp_close_cancellation_is_not_swallowed_after_oserror(
        monkeypatch, primary_step):
    store, files = _store(monkeypatch)
    primary = OSError(f"primary temp {primary_step} failure")
    cancellation = asyncio.CancelledError("temp close was cancelled")
    fd_paths: dict[int, Path] = {}
    close_calls: list[int] = []

    def open_file(path: Path, _flags: int, _mode: int) -> int:
        fd_paths[81] = Path(path)
        files.files[Path(path)] = b""
        return 81

    def write_file(fd: int, data: memoryview) -> int:
        path = fd_paths[fd]
        if primary_step == "write":
            files.files[path] += bytes(data[:3])
            raise primary
        files.files[path] += bytes(data)
        return len(data)

    def fsync_file(_fd: int) -> None:
        if primary_step == "fsync":
            raise primary

    def close_file(fd: int) -> None:
        close_calls.append(fd)
        raise cancellation

    monkeypatch.setattr(copies, "_write_synced", _REAL_WRITE_SYNCED)
    monkeypatch.setattr(copies.os, "open", open_file)
    monkeypatch.setattr(copies.os, "write", write_file)
    monkeypatch.setattr(copies.os, "fsync", fsync_file)
    monkeypatch.setattr(copies.os, "close", close_file)

    with pytest.raises(asyncio.CancelledError) as caught:
        store.create("r1", "log-copy-r1.log")

    assert caught.value is cancellation
    notes = "\n".join(getattr(cancellation, "__notes__", ()))
    assert str(primary) in notes
    assert fd_paths[81].name in notes
    assert "temp_cleanup=not_attempted" in notes
    assert "close was cancelled" in notes
    assert files.files[fd_paths[81]]
    assert close_calls == [81]
    assert files.unlink_paths == []


@pytest.mark.parametrize("operation", ["save", "delete"])
def test_directory_close_cancellation_is_not_swallowed_after_sync_oserror(
        monkeypatch, operation):
    store, files = _store(monkeypatch)
    if operation == "delete":
        _seed_marker(files)
    primary = OSError("primary directory fsync failure")
    cancellation = asyncio.CancelledError("directory close was cancelled")
    open_calls: list[Path] = []
    fsync_calls: list[int] = []
    close_calls: list[int] = []

    def open_directory(path: Path, _flags: int) -> int:
        open_calls.append(Path(path))
        return 82

    def fsync_directory(fd: int) -> None:
        fsync_calls.append(fd)
        raise primary

    def close_directory(fd: int) -> None:
        close_calls.append(fd)
        raise cancellation

    monkeypatch.setattr(copies, "_DIRECTORY_SYNC_SUPPORTED", True)
    monkeypatch.setattr(copies, "_fsync_directory", _REAL_FSYNC_DIRECTORY)
    monkeypatch.setattr(copies.os, "open", open_directory)
    monkeypatch.setattr(copies.os, "fsync", fsync_directory)
    monkeypatch.setattr(copies.os, "close", close_directory)

    if operation == "save":
        with pytest.raises(asyncio.CancelledError) as caught:
            store.create("r1", "log-copy-r1.log")
        assert _MARKER in files.files
        assert files.temp_paths() == set()
        assert files.unlink_paths == []
    else:
        with pytest.raises(asyncio.CancelledError) as caught:
            store.delete()
        assert _MARKER not in files.files
        assert files.unlink_paths == [_MARKER]

    assert caught.value is cancellation
    notes = "\n".join(getattr(cancellation, "__notes__", ()))
    assert str(primary) in notes
    assert "directory sync not confirmed" in notes
    assert "directory close unknown" in notes
    if operation == "save":
        assert "placed_unsynced" in notes
        assert "temp_cleanup=not_present" in notes
    else:
        assert "delete_unsynced" in notes
        assert "marker absent" in notes
    assert open_calls == [_LOGS]
    assert fsync_calls == [82]
    assert close_calls == [82]


@pytest.mark.parametrize("failure_step", ["open", "fsync"])
def test_delete_program_failure_only_notes_confirmed_close_facts(
        monkeypatch, failure_step):
    store, files = _store(monkeypatch)
    _seed_marker(files)
    primary = RuntimeError(f"directory {failure_step} program failure")
    open_calls: list[Path] = []
    fsync_calls: list[int] = []
    close_calls: list[int] = []

    def open_directory(path: Path, _flags: int) -> int:
        open_calls.append(Path(path))
        if failure_step == "open":
            raise primary
        return 86

    def fsync_directory(fd: int) -> None:
        fsync_calls.append(fd)
        if failure_step == "fsync":
            raise primary

    def close_directory(fd: int) -> None:
        close_calls.append(fd)

    monkeypatch.setattr(copies, "_DIRECTORY_SYNC_SUPPORTED", True)
    monkeypatch.setattr(copies, "_fsync_directory", _REAL_FSYNC_DIRECTORY)
    monkeypatch.setattr(copies.os, "open", open_directory)
    monkeypatch.setattr(copies.os, "fsync", fsync_directory)
    monkeypatch.setattr(copies.os, "close", close_directory)

    with pytest.raises(RuntimeError) as caught:
        store.delete()

    assert caught.value is primary
    assert _MARKER not in files.files
    assert files.unlink_paths == [_MARKER]
    notes = "\n".join(getattr(primary, "__notes__", ()))
    assert "outcome=delete_unsynced" in notes
    assert "unlink confirmed" in notes
    assert "marker absent" in notes
    assert "directory sync not confirmed" in notes
    assert "directory close unknown" not in notes
    assert open_calls == [_LOGS]
    if failure_step == "open":
        assert fsync_calls == []
        assert close_calls == []
    else:
        assert fsync_calls == [86]
        assert close_calls == [86]


def test_temp_cleanup_cancellation_propagates_after_save_oserror(monkeypatch):
    store, files = _store(monkeypatch)
    _seed_marker(files)
    primary = OSError("primary marker write failure")
    cancellation = asyncio.CancelledError("temp unlink was cancelled")

    def write_partial(path: Path, _data: bytes) -> None:
        files.files[path] = b"partial"
        raise primary

    def cancel_cleanup(_path: Path) -> None:
        raise cancellation

    files.write_hook = write_partial
    files.unlink_hook = cancel_cleanup

    with pytest.raises(asyncio.CancelledError) as caught:
        store.update_stage(
            MarkerStage.COPIED,
            expected_round_id="r1",
            expected_copy_name="log-copy-r1.log",
        )

    assert caught.value is cancellation
    notes = "\n".join(getattr(cancellation, "__notes__", ()))
    assert str(primary) in notes
    assert files.write_paths[0].name in notes
    assert "failed_not_created" in notes
    assert "temp_cleanup=unknown" in notes
    assert "cleanup_exception=CancelledError: temp unlink was cancelled" in notes
    assert files.temp_paths() == {files.write_paths[0]}
    assert files.unlink_paths == [files.write_paths[0]]


@pytest.mark.parametrize("primary_step", ["write", "fsync"])
def test_program_write_error_keeps_identity_when_close_oserror(
        monkeypatch, primary_step):
    store, files = _store(monkeypatch)
    primary = RuntimeError(f"primary program {primary_step} failure")
    close_error = OSError("secondary temp close result unknown")
    fd_paths: dict[int, Path] = {}
    close_calls: list[int] = []

    def open_file(path: Path, _flags: int, _mode: int) -> int:
        fd_paths[83] = Path(path)
        files.files[Path(path)] = b""
        return 83

    def write_file(fd: int, data: memoryview) -> int:
        if primary_step == "write":
            files.files[fd_paths[fd]] += bytes(data[:3])
            raise primary
        files.files[fd_paths[fd]] += bytes(data)
        return len(data)

    def fsync_file(_fd: int) -> None:
        if primary_step == "fsync":
            raise primary

    def close_file(fd: int) -> None:
        close_calls.append(fd)
        raise close_error

    monkeypatch.setattr(copies, "_write_synced", _REAL_WRITE_SYNCED)
    monkeypatch.setattr(copies.os, "open", open_file)
    monkeypatch.setattr(copies.os, "write", write_file)
    monkeypatch.setattr(copies.os, "fsync", fsync_file)
    monkeypatch.setattr(copies.os, "close", close_file)

    with pytest.raises(RuntimeError) as caught:
        store.create("r1", "log-copy-r1.log")

    assert caught.value is primary
    assert files.files[fd_paths[83]]
    assert close_calls == [83]
    assert files.unlink_paths == []
    notes = "\n".join(getattr(primary, "__notes__", ()))
    assert str(close_error) in notes
    assert fd_paths[83].name in notes
    assert "temp_cleanup=not_attempted" in notes


def test_zero_byte_write_stops_after_no_progress(monkeypatch):
    store, files = _store(monkeypatch)
    fd_paths: dict[int, Path] = {}
    write_calls: list[int] = []

    def open_file(path: Path, _flags: int, _mode: int) -> int:
        fd_paths[73] = Path(path)
        files.files[Path(path)] = b""
        return 73

    def write_zero_then_sentinel(_fd: int, _data: memoryview) -> int:
        write_calls.append(73)
        if len(write_calls) == 1:
            return 0
        raise OSError("a second write followed zero progress")

    monkeypatch.setattr(copies, "_write_synced", _REAL_WRITE_SYNCED)
    monkeypatch.setattr(copies.os, "open", open_file)
    monkeypatch.setattr(copies.os, "write", write_zero_then_sentinel)
    monkeypatch.setattr(copies.os, "fsync", lambda _fd: None)
    monkeypatch.setattr(copies.os, "close", lambda _fd: None)

    result = store.create("r1", "log-copy-r1.log")

    assert result.outcome is SaveOutcome.FAILED_NOT_CREATED
    assert len(write_calls) == 1
    _assert_temp_facts(result, fd_paths[73].name, DeleteOutcome.DELETED)
    assert files.temp_paths() == set()
    assert files.unlink_paths == [fd_paths[73]]


def test_unexpected_save_error_survives_cleanup_program_error(monkeypatch):
    store, files = _store(monkeypatch)
    primary = RuntimeError("marker encoder failed")

    def write_then_raise(path: Path, _data: bytes) -> None:
        files.files[path] = b"partial"
        raise primary

    def cleanup_program_error(_path: Path) -> None:
        raise RuntimeError("cleanup implementation failed")

    files.write_hook = write_then_raise
    files.unlink_hook = cleanup_program_error

    with pytest.raises(RuntimeError) as caught:
        store.create("r1", "log-copy-r1.log")

    assert caught.value is primary
    assert any(
        "cleanup implementation failed" in note
        for note in getattr(caught.value, "__notes__", ())
    )
    assert files.unlink_paths == [files.write_paths[0]]


@pytest.mark.parametrize("cleanup_program_error", [False, True])
def test_cancelled_save_propagates_same_exception_after_cleanup(
        monkeypatch, cleanup_program_error):
    store, files = _store(monkeypatch)
    cancellation = asyncio.CancelledError("marker save cancelled")

    def write_then_cancel(path: Path, _data: bytes) -> None:
        files.files[path] = b"partial"
        raise cancellation

    if cleanup_program_error:
        def fail_cleanup(_path: Path) -> None:
            raise RuntimeError("cancel cleanup program error")

        files.unlink_hook = fail_cleanup
    files.write_hook = write_then_cancel

    with pytest.raises(asyncio.CancelledError) as caught:
        store.create("r1", "log-copy-r1.log")

    assert caught.value is cancellation
    notes = "\n".join(getattr(caught.value, "__notes__", ()))
    assert "marker_save_facts" in notes
    if cleanup_program_error:
        assert "cancel cleanup program error" in notes
        assert files.temp_paths() == {files.write_paths[0]}
    else:
        assert "temp_cleanup=deleted" in notes
        assert files.temp_paths() == set()


def test_cleanup_program_error_without_primary_keeps_save_facts(monkeypatch):
    _store(monkeypatch)
    temp = _LOGS / "failure-marker.json.confirmed.tmp"
    cleanup_failure = RuntimeError("cleanup without primary save error")

    def fail_cleanup(_path: Path) -> None:
        raise cleanup_failure

    monkeypatch.setattr(copies, "_unlink", fail_cleanup)
    with pytest.raises(RuntimeError) as caught:
        copies._cleanup_marker_temp(temp, save_outcome=SaveOutcome.SAVED)

    assert caught.value is cleanup_failure
    notes = "\n".join(getattr(caught.value, "__notes__", ()))
    assert "outcome=saved" in notes
    assert temp.name in notes


def test_save_oserror_and_cleanup_program_error_propagate_in_order(monkeypatch):
    store, files = _store(monkeypatch)
    primary = OSError("primary marker write error")
    cleanup_failure = RuntimeError("cleanup implementation failed")

    def write_partial(path: Path, _data: bytes) -> None:
        files.files[path] = b"partial"
        raise primary

    def cleanup_program_error(_path: Path) -> None:
        raise cleanup_failure

    files.write_hook = write_partial
    files.unlink_hook = cleanup_program_error

    with pytest.raises(ExceptionGroup) as caught:
        store.create("r1", "log-copy-r1.log")

    assert caught.value.exceptions == (primary, cleanup_failure)
    notes = "\n".join(getattr(caught.value, "__notes__", ()))
    assert "failed_not_created" in notes
    assert files.write_paths[0].name in notes
    assert files.unlink_paths == [files.write_paths[0]]


def test_unconfirmed_close_does_not_attempt_unlink_and_keeps_facts(monkeypatch):
    store, files = _store(monkeypatch)
    fd_paths: dict[int, Path] = {}
    close_calls: list[int] = []

    def open_file(path: Path, _flags: int, _mode: int) -> int:
        fd_paths[74] = Path(path)
        files.files[Path(path)] = b""
        return 74

    def write_file(fd: int, data: memoryview) -> int:
        path = fd_paths[fd]
        files.files[path] += bytes(data)
        return len(data)

    def close_file(fd: int) -> None:
        close_calls.append(fd)
        raise OSError("close result unconfirmed")

    monkeypatch.setattr(copies, "_write_synced", _REAL_WRITE_SYNCED)
    monkeypatch.setattr(copies.os, "open", open_file)
    monkeypatch.setattr(copies.os, "write", write_file)
    monkeypatch.setattr(copies.os, "fsync", lambda _fd: None)
    monkeypatch.setattr(copies.os, "close", close_file)

    result = store.create("r1", "log-copy-r1.log")

    assert result.outcome is SaveOutcome.FAILED_NOT_CREATED
    assert "close result unconfirmed" in result.error
    _assert_temp_facts(result, fd_paths[74].name, None)
    assert fd_paths[74] in files.files
    assert close_calls == [74]
    assert files.unlink_paths == []


def test_session_diagnostic_includes_typed_marker_cleanup_failure():
    save = MarkerSave(
        SaveOutcome.FAILED_NOT_CREATED,
        "primary marker write error",
        temp_name="failure-marker.json.owned.tmp",
        temp_cleanup=MarkerDelete(
            DeleteOutcome.FAILED, "secondary temp unlink error"),
    )
    receipt = CopyReceipt(
        CopyOutcome.MARKER_CREATE_FAILED,
        round_id="r1",
        copy_name="log-copy-r1.log",
        marker_create=save,
    )

    diagnostic, stderr_only = _copy_receipt_diagnostic(
        RuntimeError("report generation failed"), receipt)

    assert stderr_only is False
    assert diagnostic is not None
    assert "primary marker write error" in diagnostic
    assert "failure-marker.json.owned.tmp" in diagnostic
    assert "secondary temp unlink error" in diagnostic


@pytest.mark.parametrize(
    ("consumer", "stage"),
    [
        ("marker_create", None),
        ("marker_progress", MarkerStage.COPIED),
        ("marker_progress", MarkerStage.PUBLISHED),
    ],
)
def test_session_diagnostic_keeps_saved_close_error_for_each_consumer(
        consumer, stage):
    save = MarkerSave(
        SaveOutcome.SAVED,
        "dir_close_failed_after_sync: OSError: marker directory close error",
        temp_name="failure-marker.json.saved.tmp",
        temp_cleanup=MarkerDelete(DeleteOutcome.NOT_PRESENT),
    )
    receipt = CopyReceipt(
        CopyOutcome.PUBLISHED,
        round_id="r1",
        copy_name="log-copy-r1.log",
        marker_create=(save if consumer == "marker_create" else None),
        marker_progress=(() if stage is None else ((stage, save),)),
    )

    diagnostic, stderr_only = _copy_receipt_diagnostic(
        RuntimeError("report generation failed"), receipt)

    assert stderr_only is False
    assert diagnostic is not None
    expected_stage = ("marker_create" if stage is None
                      else f"marker_progress[{stage.value}]")
    assert f"{expected_stage}: outcome=saved" in diagnostic
    assert "marker directory close error" in diagnostic
    assert "temp_cleanup=not_present" in diagnostic


def test_session_diagnostic_keeps_cleanup_failure_without_save_error():
    save = MarkerSave(
        SaveOutcome.SAVED,
        error=None,
        temp_name="failure-marker.json.saved.tmp",
        temp_cleanup=MarkerDelete(
            DeleteOutcome.FAILED, "saved temp cleanup permission denied"),
    )
    receipt = CopyReceipt(
        CopyOutcome.PUBLISHED,
        round_id="r1",
        copy_name="log-copy-r1.log",
        marker_create=save,
    )

    diagnostic, _ = _copy_receipt_diagnostic(
        RuntimeError("report generation failed"), receipt)

    assert diagnostic is not None
    assert "marker_create: outcome=saved; error=none" in diagnostic
    assert "temp_cleanup=failed" in diagnostic
    assert "saved temp cleanup permission denied" in diagnostic


@pytest.mark.parametrize("failure", [
    RuntimeError("marker save raised"),
    ExceptionGroup("marker save and cleanup", [
        OSError("primary write failure"),
        OSError("secondary cleanup failure"),
    ]),
])
def test_failure_exception_diagnostic_keeps_notes_and_group_members(failure):
    if isinstance(failure, RuntimeError):
        failure.add_note("temp cleanup: unlink failed for owned marker")
    else:
        failure.exceptions[0].add_note("write handle closed")
        failure.exceptions[1].add_note("owned temp remains")

    diagnostic = _failure_copy_exception(
        "delivery_call", RuntimeError("report failed"), failure)

    assert "primary write failure" in diagnostic or "marker save raised" in diagnostic
    assert "secondary cleanup failure" in diagnostic or "owned marker" in diagnostic


def test_failure_exception_diagnostic_includes_cause_chain():
    cause = OSError("temporary file close failure")
    failure = RuntimeError("marker writer failed")
    failure.__cause__ = cause

    diagnostic = _failure_copy_exception(
        "delivery_call", RuntimeError("report failed"), failure)

    assert "marker writer failed" in diagnostic
    assert "temporary file close failure" in diagnostic
