"""视频分段的文件身份特化，公共集合不依赖某一设备的名称解析器。"""

from dataclasses import dataclass
from enum import StrEnum
import posixpath
import re

from camctl.devices.segments import SegmentKey


class SegmentFileKind(StrEnum):
    VIDEO = "video"
    PREVIEW = "preview"
    PHOTO = "photo"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class SegmentFileFacts:
    kind: SegmentFileKind
    key: SegmentKey | None = None
    pairing_identity: str | None = None


@dataclass(frozen=True, slots=True)
class SegmentProfile:
    directory_prefix: str
    video_suffix: str
    allow_empty_tail: bool
    photo_suffixes: frozenset[str] = frozenset()

    def classify_file(self, identity: str) -> SegmentFileFacts:
        if not isinstance(identity, str):
            return SegmentFileFacts(SegmentFileKind.OTHER)
        suffix = posixpath.splitext(identity)[1].casefold()
        if suffix in self.photo_suffixes:
            return SegmentFileFacts(SegmentFileKind.PHOTO)
        kind = (SegmentFileKind.VIDEO if suffix == self.video_suffix else
                SegmentFileKind.PREVIEW if suffix == ".lrf" else SegmentFileKind.OTHER)
        if kind is SegmentFileKind.OTHER:
            return SegmentFileFacts(kind)
        if (not identity.startswith("/") or "\0" in identity
                or posixpath.normpath(identity) != identity
                or any(part in {".", ".."} for part in identity.split("/"))):
            return SegmentFileFacts(kind)
        directory = posixpath.basename(posixpath.dirname(identity))
        prefix = re.escape(self.directory_prefix)
        folder = re.fullmatch(prefix + r"_([0-9]{3,})", directory)
        name = re.fullmatch(
            "(?i:" + prefix + r")_[0-9]{14}_([0-9]{4})_D\.[^.]+",
            posixpath.basename(identity),
        )
        if folder is None or name is None:
            return SegmentFileFacts(kind)
        try:
            number = int(folder[1])
            if directory != f"{self.directory_prefix}_{number:03d}":
                return SegmentFileFacts(kind)
            key = SegmentKey(number, int(name[1]))
        except ValueError:
            return SegmentFileFacts(kind)
        return SegmentFileFacts(kind, key, identity[:-len(suffix)])


_OSMO = SegmentProfile("CAM", ".osv", True)
_ACTION6 = SegmentProfile("DJI", ".mp4", False, frozenset({".dng", ".jpg", ".jpeg"}))


def profile_for_policy(policy: str) -> SegmentProfile:
    from camctl.devices.tasks import CaptureResultPolicy

    if policy in (CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1,
                  CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1):
        return _OSMO
    if policy == "action6_video_segments_v1":
        return _ACTION6
    raise ValueError(f"未登记的视频分段特化: {policy!r}")
