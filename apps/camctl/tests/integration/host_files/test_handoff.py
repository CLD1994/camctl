"""F5 原子交接与撤回和真实目录的组合集成测试。

真实文件系统验证原子发布、同名不覆盖、源缺失竞争及撤回边界；
模拟主程序领取（ready → processing）验证已移动事实不因领取撤销。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from camctl.host_files.handoff import (
    HandoffDirectories,
    HandoffIdentity,
    PublishStage,
    ReadyName,
    WithdrawStage,
    publish_file,
    withdraw_file,
)
from camctl.host_files.io import DirectorySyncStage
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef


def _setup(tmp_path: Path, content: bytes = b"payload") -> tuple[
    FileRef, BoundDirectories, HandoffDirectories, Path, Path, Path
]:
    staging = tmp_path / "staging"
    ready = tmp_path / "ready"
    processing = tmp_path / "processing"
    (staging / "deliveries").mkdir(parents=True)
    ready.mkdir()
    processing.mkdir()
    source = staging / "deliveries" / "7.bin"
    source.write_bytes(content)
    ref = FileRef(
        file_id=7,
        purpose=FilePurpose.DELIVERY_COPY,
        relative_path="deliveries/7.bin",
        root=staging,
    )
    roots = BoundDirectories(staging=staging)
    return ref, roots, HandoffDirectories(staging=staging, ready=ready), source, ready, processing


pytestmark = pytest.mark.asyncio


async def test_real_publish_moves_atomically(tmp_path: Path) -> None:
    ref, roots, dirs, source, ready, _ = _setup(tmp_path, b"first")
    result = await publish_file(ref, roots, dirs, ReadyName("7.bin"))
    assert result.stage is PublishStage.MOVED
    assert result.error is None
    assert result.source_removed is True
    assert (ready / "7.bin").read_bytes() == b"first"
    assert not source.exists()

    # 同名目标不覆盖：重新准备同源再发布被拒绝，原内容保持。
    source.write_bytes(b"second")
    again = await publish_file(ref, roots, dirs, ReadyName("7.bin"))
    assert again.stage is PublishStage.NOT_MOVED
    assert "target_exists" in (again.error or "")
    assert (ready / "7.bin").read_bytes() == b"first"
    assert source.read_bytes() == b"second"


async def test_source_missing_before_move(tmp_path: Path) -> None:
    ref, roots, dirs, source, ready, _ = _setup(tmp_path)
    source.unlink()
    result = await publish_file(ref, roots, dirs, ReadyName("7.bin"))
    assert result.stage is PublishStage.NOT_MOVED
    assert "source_missing" in (result.error or "")
    assert not (ready / "7.bin").exists()


async def test_directory_sync_failure_keeps_moved_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import camctl.host_files.handoff as handoff_module

    ref, roots, dirs, source, ready, _ = _setup(tmp_path)

    def fail_sync(directory: Path) -> None:
        raise OSError(5, "io")

    monkeypatch.setattr(handoff_module, "_DIRECTORY_SYNC_SUPPORTED", True)
    monkeypatch.setattr(handoff_module, "_sync_directory", fail_sync)
    result = await publish_file(ref, roots, dirs, ReadyName("7.bin"))
    assert result.stage is PublishStage.MOVED
    assert result.directory is DirectorySyncStage.FAILED
    assert result.error is not None
    # 文件已可被领取：已移动事实不因同步失败撤销。
    assert (ready / "7.bin").exists()


async def test_claimed_file_is_not_modified_by_withdraw(tmp_path: Path) -> None:
    ref, roots, dirs, source, ready, processing = _setup(tmp_path, b"claimed")
    result = await publish_file(ref, roots, dirs, ReadyName("7.bin"))
    assert result.stage is PublishStage.MOVED
    # 模拟 C 领取模块：主程序把 ready 文件移动到 processing。
    shutil.move(str(ready / "7.bin"), str(processing / "7.bin"))
    withdraw = await withdraw_file(
        HandoffIdentity(ready_dir=ready, name="7.bin")
    )
    assert withdraw.stage is WithdrawStage.NOT_PRESENT
    assert (processing / "7.bin").read_bytes() == b"claimed"


async def test_real_withdraw_removes_ready_file(tmp_path: Path) -> None:
    ref, roots, dirs, source, ready, processing = _setup(tmp_path, b"bye")
    await publish_file(ref, roots, dirs, ReadyName("7.bin"))
    first = await withdraw_file(HandoffIdentity(ready_dir=ready, name="7.bin"))
    assert first.stage is WithdrawStage.WITHDRAWN
    assert not (ready / "7.bin").exists()
    second = await withdraw_file(HandoffIdentity(ready_dir=ready, name="7.bin"))
    assert second.stage is WithdrawStage.NOT_PRESENT
