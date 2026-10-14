"""启动日志清理对真实文件分类和本次归属的集成回归。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from camctl.logging_runtime import copies
from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.copies import (
    MARKER_NAME,
    CopyOutcome,
    CopyRequest,
    DeleteOutcome,
    MarkerPresence,
    MarkerStage,
    MarkerStore,
    RunFileOwnership,
    SaveOutcome,
    copy_file_name,
    marker_temp_file_name,
    startup_sweep,
)
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord


def _logs_dir(tmp_path: Path) -> Path:
    logs = tmp_path / "staging" / "logs"
    logs.mkdir(parents=True)
    return logs


def test_unconfirmed_old_owner_files_wait_for_settled_sweep(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    previous_owner = RunFileOwnership()
    current_owner = RunFileOwnership()
    old_copy_name = copy_file_name(previous_owner.new_token())
    old_temp_name = marker_temp_file_name(previous_owner.new_token())
    old_copy = logs / old_copy_name
    old_temp = logs / old_temp_name
    old_copy.write_bytes(b"old partial log copy")
    old_temp.write_bytes(b"old partial marker")
    assert previous_owner.owns_name(old_copy.name)
    assert previous_owner.owns_name(old_temp.name)
    assert not current_owner.owns_name(old_copy.name)
    assert not current_owner.owns_name(old_temp.name)

    real_unlink = copies._unlink
    unlink_attempts: list[Path] = []

    def record_unlink(path: Path) -> None:
        unlink_attempts.append(Path(path))
        real_unlink(path)

    monkeypatch.setattr(copies, "_unlink", record_unlink)

    unconfirmed = startup_sweep(
        logs, ownership=current_owner, owner_settled=False)

    assert unconfirmed.error is None and unconfirmed.failures == ()
    assert old_copy.is_file() and old_temp.is_file()
    assert unlink_attempts == []

    settled = startup_sweep(
        logs, ownership=current_owner, owner_settled=True)

    assert settled.error is None and settled.failures == ()
    assert not old_copy.exists() and not old_temp.exists()
    assert set(unlink_attempts) == {old_copy, old_temp}
    assert len(unlink_attempts) == 2

    completed = startup_sweep(
        logs, ownership=current_owner, owner_settled=True)

    assert completed.error is None and completed.failures == ()
    assert len(unlink_attempts) == 2


@pytest.mark.parametrize("marker_case", ["valid", "invalid", "unreadable"])
def test_sweep_preserves_formal_marker_and_other_log_scopes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        marker_case: str) -> None:
    logs = _logs_dir(tmp_path)
    ready = tmp_path / "ready"
    processing = tmp_path / "processing"
    ready.mkdir()
    processing.mkdir()

    marker_owner = RunFileOwnership()
    marker_round = marker_owner.new_token()
    marker_copy_name = copy_file_name(marker_owner.new_token())
    marker_store = MarkerStore(logs, ownership=marker_owner)
    assert marker_store.create(marker_round, marker_copy_name).outcome is (
        SaveOutcome.SAVED)
    marker_path = logs / MARKER_NAME
    if marker_case == "invalid":
        marker_path.write_bytes(b"{not a marker document")

    unknown = logs / "notes.txt"
    unknown.write_bytes(b"unknown purpose")
    ready_copy = ready / copy_file_name(RunFileOwnership().new_token())
    processing_copy = processing / copy_file_name(RunFileOwnership().new_token())
    ready_copy.write_bytes(b"ready-owned copy")
    processing_copy.write_bytes(b"processing-owned copy")

    channel = FileChannel(logs / "ordinary.log", max_bytes=4096, file_count=2)
    try:
        write_result = channel.write_record(
            LogRecord(level=LogLevel.INFO, message="ordinary log remains"))
        assert write_result.appended is True
        ordinary_log = logs / "ordinary.log"
        ordinary_bytes = ordinary_log.read_bytes()
        marker_bytes = marker_path.read_bytes()
    finally:
        channel.close()

    unreadable_reads: list[Path] = []
    if marker_case == "unreadable":
        real_read = copies._read_bytes

        def deny_marker_read(path: Path) -> bytes:
            target = Path(path)
            if target == marker_path:
                unreadable_reads.append(target)
                raise PermissionError("controlled formal marker read denial")
            return real_read(path)

        monkeypatch.setattr(copies, "_read_bytes", deny_marker_read)
        unreadable = marker_store.check()
        assert unreadable.presence is MarkerPresence.EXISTS
        assert "controlled formal marker read denial" in (unreadable.error or "")
        assert unreadable_reads == [marker_path]

    result = startup_sweep(
        logs, ownership=RunFileOwnership(), owner_settled=True)

    assert result.error is None and result.failures == ()
    assert marker_path.is_file() and marker_path.read_bytes() == marker_bytes
    assert unknown.read_bytes() == b"unknown purpose"
    assert ready_copy.read_bytes() == b"ready-owned copy"
    assert processing_copy.read_bytes() == b"processing-owned copy"
    assert ordinary_log.read_bytes() == ordinary_bytes
    if marker_case == "unreadable":
        # 扫描保留正式标记，不读取其内容作为删除授权条件。
        assert unreadable_reads == [marker_path]
    if marker_case == "valid":
        assert marker_store.check().state is not None
    elif marker_case == "invalid":
        invalid = marker_store.check()
        assert invalid.presence is MarkerPresence.EXISTS
        assert invalid.state is None


def test_sweep_keeps_fifo_candidate_without_opening_it(
        tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("当前平台不支持 FIFO 文件")

    logs = _logs_dir(tmp_path)
    old_owner = RunFileOwnership()
    fifo = logs / copy_file_name(old_owner.new_token())
    regular = logs / copy_file_name(old_owner.new_token())
    try:
        os.mkfifo(fifo)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"当前临时文件系统不支持 FIFO: {error}")
    regular.write_bytes(b"settled regular copy")

    result = startup_sweep(
        logs, ownership=RunFileOwnership(), owner_settled=True)

    assert result.error is None and result.failures == ()
    assert stat.S_ISFIFO(fifo.lstat().st_mode)
    assert not regular.exists()


def test_sweep_reports_candidate_errors_and_retries_only_next_invocation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = _logs_dir(tmp_path)
    ready = tmp_path / "ready"
    ready.mkdir()
    previous_owner = RunFileOwnership()
    stale_lstat = logs / copy_file_name(previous_owner.new_token())
    stale_unlink = logs / marker_temp_file_name(previous_owner.new_token())
    clean_copy = logs / copy_file_name(previous_owner.new_token())
    clean_temp = logs / marker_temp_file_name(previous_owner.new_token())
    stale_lstat.write_bytes(b"lstat uncertain")
    stale_unlink.write_bytes(b"unlink uncertain")
    clean_copy.write_bytes(b"clean old copy")
    clean_temp.write_bytes(b"clean old temp")

    real_lstat = copies._lstat
    real_unlink = copies._unlink
    lstat_attempts: list[Path] = []
    unlink_attempts: list[Path] = []

    def fail_one_lstat(path: Path):
        target = Path(path)
        if target == stale_lstat:
            lstat_attempts.append(target)
            if len(lstat_attempts) == 1:
                raise PermissionError("controlled candidate lstat denial")
        return real_lstat(path)

    def fail_one_unlink(path: Path) -> None:
        target = Path(path)
        if target == stale_unlink:
            unlink_attempts.append(target)
            if len(unlink_attempts) == 1:
                raise PermissionError("controlled candidate unlink denial")
        real_unlink(path)

    monkeypatch.setattr(copies, "_lstat", fail_one_lstat)
    monkeypatch.setattr(copies, "_unlink", fail_one_unlink)

    first = startup_sweep(
        logs, ownership=RunFileOwnership(), owner_settled=True)

    first_failures = dict(first.failures)
    assert first.error is None
    assert set(first_failures) == {stale_lstat.name, stale_unlink.name}
    assert "lstat_failed" in first_failures[stale_lstat.name]
    assert "unlink_failed" in first_failures[stale_unlink.name]
    assert stale_lstat.is_file() and stale_unlink.is_file()
    assert not clean_copy.exists() and not clean_temp.exists()
    assert ready.is_dir() and list(ready.iterdir()) == []
    assert lstat_attempts == [stale_lstat]
    assert unlink_attempts == [stale_unlink]

    # 新一轮的独立扫描可以清理上次留下的候选；本轮没有重试或复制。
    next_invocation = startup_sweep(
        logs, ownership=RunFileOwnership(), owner_settled=True)

    assert next_invocation.error is None and next_invocation.failures == ()
    assert not stale_lstat.exists() and not stale_unlink.exists()
    assert lstat_attempts == [stale_lstat, stale_lstat]
    assert unlink_attempts == [stale_unlink, stale_unlink]
    assert ready.is_dir() and list(ready.iterdir()) == []


@pytest.mark.asyncio
async def test_real_copy_and_closed_failed_marker_temp_share_run_ownership(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staging = tmp_path / "staging"
    logs = staging / "logs"
    ready = tmp_path / "ready"
    logs.mkdir(parents=True)
    ready.mkdir()

    ownership = RunFileOwnership()
    marker = MarkerStore(logs, ownership=ownership)
    channel = FileChannel(tmp_path / "camctl.log", max_bytes=4096, file_count=2)
    try:
        initial_write = channel.write_record(
            LogRecord(level=LogLevel.INFO, message="copy context"))
        assert initial_write.appended is True
    except BaseException:
        channel.close()
        raise

    real_write_synced = copies._write_synced
    synced_temp_paths: list[Path] = []

    def record_synced_temp(path: Path, data: bytes) -> None:
        real_write_synced(path, data)
        # _write_synced 返回意味着真实 write、fsync 和 close 都已返回。
        synced_temp_paths.append(Path(path))

    monkeypatch.setattr(copies, "_write_synced", record_synced_temp)

    async def fail_before_handoff(_source, _directories, _target):
        raise OSError("controlled handoff failure")

    request = CopyRequest(
        trigger=LogRecord(level=LogLevel.ERROR, message="owned copy trigger"),
        staging_root=staging,
        ready_dir=ready,
        channel=channel,
        marker=marker,
    )
    real_replace = copies._replace
    real_unlink = copies._unlink
    failed_temp_paths: list[Path] = []
    failed_temp_cleanup: list[Path] = []

    try:
        receipt = await copies.deliver_failure_copy(
            request, publish=fail_before_handoff)

        assert receipt.outcome is CopyOutcome.COPY_UNKNOWN
        assert receipt.stage is MarkerStage.COPIED
        assert receipt.copy_name is not None
        assert ownership.owns_name(receipt.copy_name)
        assert receipt.write_result is not None
        assert receipt.write_result.copy_handles_closed is True
        assert receipt.write_result.copy_source_close_error is None
        assert receipt.write_result.copy_target_close_error is None
        current_copy = logs / receipt.copy_name
        assert current_copy.is_file()
        assert b"owned copy trigger" in current_copy.read_bytes()
        assert list(ready.iterdir()) == []
        assert marker.ownership is ownership
        assert synced_temp_paths
        assert all(ownership.owns_name(path.name) for path in synced_temp_paths)

        def fail_marker_placement(source: Path, target: Path) -> None:
            path = Path(source)
            assert Path(target) == logs / MARKER_NAME
            assert path in synced_temp_paths
            failed_temp_paths.append(path)
            raise OSError("controlled marker placement failure")

        def fail_failed_temp_cleanup(path: Path) -> None:
            target = Path(path)
            if failed_temp_paths and target == failed_temp_paths[0]:
                failed_temp_cleanup.append(target)
                raise OSError("controlled closed marker temp cleanup failure")
            real_unlink(path)

        monkeypatch.setattr(copies, "_replace", fail_marker_placement)
        monkeypatch.setattr(copies, "_unlink", fail_failed_temp_cleanup)
        failed_save = marker.update_stage(
            MarkerStage.PUBLISHED,
            expected_round_id=receipt.round_id,
            expected_copy_name=receipt.copy_name,
        )

        assert failed_save.outcome is SaveOutcome.UNKNOWN
        assert failed_save.temp_name == failed_temp_paths[0].name
        assert failed_save.temp_cleanup is not None
        assert failed_save.temp_cleanup.outcome is DeleteOutcome.FAILED
        assert failed_temp_cleanup == failed_temp_paths
        assert failed_temp_paths[0] in synced_temp_paths
        assert failed_temp_paths[0].is_file()
        assert ownership.owns_name(failed_temp_paths[0].name)
        assert marker.check().state is not None
        assert marker.check().state.stage is MarkerStage.COPIED

        previous_owner = RunFileOwnership()
        stale_copy = logs / copy_file_name(previous_owner.new_token())
        stale_temp = logs / marker_temp_file_name(previous_owner.new_token())
        stale_copy.write_bytes(b"previous run copy")
        stale_temp.write_bytes(b"previous run temp")
        assert not ownership.owns_name(stale_copy.name)
        assert not ownership.owns_name(stale_temp.name)

        swept = startup_sweep(
            logs, ownership=ownership, owner_settled=True)

        assert swept.error is None and swept.failures == ()
        assert current_copy.is_file()
        assert failed_temp_paths[0].is_file()
        assert (logs / MARKER_NAME).is_file()
        assert not stale_copy.exists() and not stale_temp.exists()
        assert list(ready.iterdir()) == []
    finally:
        channel.close()
