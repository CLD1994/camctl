"""视频分段的文件身份特化，公共集合不依赖某一设备的名称解析器。"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
import posixpath
import re
from collections.abc import Mapping

from camctl.devices.segments import SegmentKey
from camctl.contracts.video_coverage import CoverageTarget, CoverageMetric, target_frames


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
    coverage_mode: str | None = None

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

    def coverage_target(
        self,
        effective_params: Mapping[str, object],
        execution_spec: Mapping[str, object],
        action_type: str,
    ) -> CoverageTarget | None:
        """按本策略从首次受理的有效参数读取目标，不访问当前部署配置。"""
        if self.coverage_mode is None:
            return None
        if self.coverage_mode != "action6":
            raise ValueError(f"未登记的覆盖目标提供方式: {self.coverage_mode!r}")
        if action_type == "camera_record":
            milliseconds = execution_spec.get("target_duration_ms")
            if type(milliseconds) is not int or milliseconds <= 0:
                raise ValueError("普通录像的冻结目标时长缺失或非法")
            return CoverageTarget(
                CoverageMetric.DURATION_S, Decimal(milliseconds) / Decimal(1000)
            )
        if action_type != "camera_timelapse":
            raise ValueError(f"Action6 分段不支持动作类型: {action_type!r}")
        duration = effective_params.get("duration_s")
        interval = effective_params.get("interval_s")
        duration = _exact_positive_decimal(duration, "采集时长")
        interval = _exact_positive_decimal(interval, "采样间隔")
        if interval >= duration:
            raise ValueError("采样间隔必须短于采集时长")
        return CoverageTarget(CoverageMetric.FRAMES, target_frames(duration, interval))


def _exact_positive_decimal(value, label: str) -> Decimal:
    if type(value) is int:
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{label}必须是正的精确数值")
    return value


_OSMO = SegmentProfile("CAM", ".osv", True)
_ACTION6 = SegmentProfile("DJI", ".mp4", False, frozenset({".dng", ".jpg", ".jpeg"}), "action6")


def profile_for_policy(policy: str) -> SegmentProfile:
    from camctl.devices.tasks import CaptureResultPolicy

    if policy in (CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1,
                  CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1):
        return _OSMO
    if policy == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1:
        return _ACTION6
    raise ValueError(f"未登记的视频分段特化: {policy!r}")
