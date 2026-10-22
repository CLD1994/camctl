"""OSMO 360 II CAM 目录与文件名的有序分段解析。"""

from dataclasses import dataclass
import posixpath
from camctl.devices.segment_profiles import profile_for_policy

from camctl.devices.segments import SegmentKey


@dataclass(frozen=True, slots=True)
class OsmoSegmentName:
    """设备文件名中可用于排序和精确配对的事实。"""

    key: SegmentKey
    pairing_identity: str
    suffix: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, SegmentKey):
            raise ValueError("OSMO 文件名必须携带分段顺序键")
        if not isinstance(self.pairing_identity, str) or not self.pairing_identity:
            raise ValueError("配对身份必须是非空文件主干")
        self.pairing_identity.encode("utf-8")
        if self.suffix not in {".osv", ".lrf"}:
            raise ValueError("OSMO 文件后缀必须标准化为 .osv 或 .lrf")


def parse_osmo_segment_name(path: str) -> OsmoSegmentName | None:
    """解析直接位于 CAM_<目录号> 下的 OSV/LRF 文件名。

    datetime 不参与排序；配对身份保留完整目录和主干的原始大小写，
    只有文件扩展名按既有契约忽略大小写。
    """
    facts = profile_for_policy("osmo360ii_record_segments_v1").classify_file(path)
    if facts.key is None:
        return None
    return OsmoSegmentName(
        key=facts.key, pairing_identity=facts.pairing_identity,
        suffix=posixpath.splitext(path)[1].casefold(),
    )


def parse_osmo_segment_key(path: str) -> SegmentKey | None:
    """只解析 CAM/index 顺序键，不确认该文件属于哪次拍摄。"""
    name = parse_osmo_segment_name(path)
    return None if name is None else name.key
