"""F1 主机文件定位与路径规则的单元测试。

期望独立来自路径字段规格：相对路径恰两段、用途目录与 purpose 相
符、文件名 ID 与文件身份一致；设备定位、用户名称、绝对路径与错
误扩展名不进入定位。纯路径规则不访问真实文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from camctl.host_files.models import (
    BoundDirectories,
    FileObservationKind,
    FilePurpose,
    FileRef,
)
from camctl.host_files.paths import inspect_file, resolve_file

_STAGING = Path("/srv/camctl/staging")
_ROOTS = BoundDirectories(staging=_STAGING)


def _ref(relative_path: str, *, file_id: int = 28, purpose: FilePurpose = FilePurpose.RECORDING_INPUT) -> FileRef:
    return FileRef(
        file_id=file_id,
        purpose=purpose,
        relative_path=relative_path,
        root=_STAGING,
    )


class TestResolveRules:
    def test_valid_reference_resolves(self) -> None:
        host = resolve_file(_ref("recording-inputs/28.mp4"), _ROOTS)
        assert host.path == _STAGING / "recording-inputs" / "28.mp4"
        assert host.file_id == 28

    def test_id_without_extension_resolves(self) -> None:
        host = resolve_file(_ref("recording-inputs/28"), _ROOTS)
        assert host.path.name == "28"

    @pytest.mark.parametrize(
        "bad",
        [
            "deliveries/28.mp4",        # 用途目录与 purpose 不符
            "recording-inputs/29.mp4",  # 文件名 ID 与身份不一致
            "recording-inputs/028.mp4", # 前导零
            "recording-inputs/..",      # 上跳段
            "../recording-inputs/28",   # 上跳首段
            "/recording-inputs/28",     # 绝对路径
            "recording-inputs/28/x",    # 额外层级
            "recording-inputs",         # 只有一段
            "recording-inputs/28.mp4 ",  # 尾部空白进入扩展名
            "recording-inputs/28.mp4/",  # 尾部分隔符
        ],
    )
    def test_illegal_relative_paths_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            resolve_file(_ref(bad), _ROOTS)

    def test_backslash_and_nul_rejected(self) -> None:
        for bad in ("recording-inputs\\28", "recording-inputs/28\x00.mp4"):
            with pytest.raises(ValueError):
                resolve_file(_ref(bad), _ROOTS)

    def test_root_mismatch_rejected(self) -> None:
        ref = FileRef(
            file_id=28,
            purpose=FilePurpose.RECORDING_INPUT,
            relative_path="recording-inputs/28.mp4",
            root=Path("/other/deployment/staging"),
        )
        with pytest.raises(ValueError, match="根目录"):
            resolve_file(ref, _ROOTS)

    def test_purpose_directory_mapping(self) -> None:
        cases = {
            FilePurpose.DELIVERY_COPY: "deliveries",
            FilePurpose.RECORDING_INPUT: "recording-inputs",
            FilePurpose.PROCESSING_TEMP: "processing-temp",
            FilePurpose.REPAIR_OUTPUT: "derived",
        }
        for purpose, directory in cases.items():
            ref = FileRef(
                file_id=5,
                purpose=purpose,
                relative_path=f"{directory}/5.bin",
                root=_STAGING,
            )
            assert resolve_file(ref, _ROOTS).path.parent.name == directory

    def test_name_length_limit(self) -> None:
        long_name = "28." + "a" * 300
        with pytest.raises(ValueError):
            resolve_file(_ref(f"recording-inputs/{long_name}"), _ROOTS)


class TestInspectRules:
    def test_device_or_user_paths_never_enter_resolution(self) -> None:
        # 设备定位与用户名称不是定位输入：引用只携带保存身份。
        ref = _ref("recording-inputs/28.mp4")
        assert not hasattr(ref, "device_path")
        assert set(ref.__dataclass_fields__) >= {
            "file_id",
            "purpose",
            "relative_path",
            "root",
        }
