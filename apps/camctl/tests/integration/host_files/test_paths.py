"""F1 主机文件定位的组件集成测试：真实目录与对象观察。

临时真实目录覆盖缺失、目录对象、不可访问（POSIX 权限）与移动后
的定位；不修改用户全局路径。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from camctl.host_files.models import (
    BoundDirectories,
    FileObservationKind,
    FilePurpose,
    FileRef,
)
from camctl.host_files.paths import inspect_file

pytestmark = pytest.mark.asyncio


def _roots(tmp_path: Path) -> BoundDirectories:
    staging = tmp_path / "staging"
    (staging / "recording-inputs").mkdir(parents=True)
    return BoundDirectories(staging=staging)


def _ref(roots: BoundDirectories, file_id: int = 28) -> FileRef:
    extension = ".mp4" if file_id != 29 else ""
    return FileRef(
        file_id=file_id,
        purpose=FilePurpose.RECORDING_INPUT,
        relative_path=f"recording-inputs/{file_id}{extension}",
        root=roots.staging,
    )


class TestRealObservations:
    async def test_missing_file_observed_as_missing(self, tmp_path: Path) -> None:
        roots = _roots(tmp_path)
        observation = await inspect_file(_ref(roots), roots)
        assert observation.kind is FileObservationKind.MISSING

    async def test_directory_object_is_type_mismatch(self, tmp_path: Path) -> None:
        roots = _roots(tmp_path)
        (roots.staging / "recording-inputs" / "30").mkdir()
        ref = FileRef(
            file_id=30,
            purpose=FilePurpose.RECORDING_INPUT,
            relative_path="recording-inputs/30",
            root=roots.staging,
        )
        observation = await inspect_file(ref, roots)
        assert observation.kind is FileObservationKind.TYPE_MISMATCH

    async def test_valid_object_observed(self, tmp_path: Path) -> None:
        roots = _roots(tmp_path)
        target = roots.staging / "recording-inputs" / "28.mp4"
        target.write_bytes(b"data")
        observation = await inspect_file(_ref(roots), roots)
        assert observation.kind is FileObservationKind.VALID_OBJECT
        assert observation.is_file is True
        assert observation.size_bytes == 4

    async def test_relocated_file_reports_missing_at_saved_location(
        self, tmp_path: Path
    ) -> None:
        # 文件被移动到其他用途目录：保存位置观察为缺失，不追踪猜测。
        roots = _roots(tmp_path)
        (roots.staging / "derived").mkdir()
        (roots.staging / "derived" / "31.mp4").write_bytes(b"data")
        ref = FileRef(
            file_id=31,
            purpose=FilePurpose.RECORDING_INPUT,
            relative_path="recording-inputs/31.mp4",
            root=roots.staging,
        )
        observation = await inspect_file(ref, roots)
        assert observation.kind is FileObservationKind.MISSING

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX 目录权限不可移植模拟")
    async def test_permission_error_is_not_missing(self, tmp_path: Path) -> None:
        roots = _roots(tmp_path)
        target = roots.staging / "recording-inputs" / "28.mp4"
        target.write_bytes(b"data")
        os.chmod(roots.staging / "recording-inputs", 0o000)
        try:
            observation = await inspect_file(_ref(roots), roots)
        finally:
            os.chmod(roots.staging / "recording-inputs", 0o755)
        assert observation.kind is FileObservationKind.ERROR
        assert observation.error is not None

    async def test_traversal_through_file_is_error_not_missing(
        self, tmp_path: Path
    ) -> None:
        # 目标路径的父级是普通文件：检查错误，不解释为缺失。
        roots = _roots(tmp_path)
        (roots.staging / "recording-inputs" / "28.mp4").write_bytes(b"data")
        ref = FileRef(
            file_id=28,
            purpose=FilePurpose.RECORDING_INPUT,
            relative_path="recording-inputs/28.mp4/x.bin",
            root=roots.staging,
        )
        from camctl.host_files.paths import PathRuleError

        with pytest.raises(PathRuleError):
            await inspect_file(ref, roots)
