"""Hasselblad 拍摄格式及原图、预览的共同文件契约。"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from camctl.host_files.models import FilePurpose


class CaptureFormat(StrEnum):
    RAW = "raw"
    JPEG = "jpeg"


@dataclass(frozen=True, slots=True)
class CaptureFormatDescription:
    value: CaptureFormat
    title: str
    original_extension: str
    original_media_type: str
    preview_extension: str
    preview_media_type: str

    @property
    def command_value(self) -> str:
        return self.value.value


CAPTURE_FORMATS: Mapping[CaptureFormat, CaptureFormatDescription] = MappingProxyType({
    CaptureFormat.RAW: CaptureFormatDescription(
        CaptureFormat.RAW, "RAW（3FR）", ".3fr", "image/x-hasselblad-3fr", ".jpg", "image/jpeg",
    ),
    CaptureFormat.JPEG: CaptureFormatDescription(
        CaptureFormat.JPEG, "JPEG", ".jpg", "image/jpeg", ".jpg", "image/jpeg",
    ),
})


def photo_file_extension(purpose: FilePurpose, capture_format: CaptureFormat) -> str:
    """按保存格式及显式用途选择分配扩展名，不从 MIME 推断角色。"""
    description = CAPTURE_FORMATS[CaptureFormat(capture_format)]
    if purpose is FilePurpose.CAPTURE_ORIGINAL:
        return description.original_extension.removeprefix(".")
    if purpose is FilePurpose.CAPTURE_PREVIEW:
        return description.preview_extension.removeprefix(".")
    raise ValueError(f"不是单张拍摄文件用途: {purpose!r}")


def photo_media_type(purpose: FilePurpose, capture_format: CaptureFormat) -> str:
    description = CAPTURE_FORMATS[CaptureFormat(capture_format)]
    if purpose is FilePurpose.CAPTURE_ORIGINAL:
        return description.original_media_type
    if purpose is FilePurpose.CAPTURE_PREVIEW:
        return description.preview_media_type
    raise ValueError(f"不是单张拍摄文件用途: {purpose!r}")
