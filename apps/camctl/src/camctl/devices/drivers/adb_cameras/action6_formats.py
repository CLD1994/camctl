"""Action6 延时原片的格式、后缀及输出选择的唯一映射。"""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class OutputSelection(StrEnum):
    VIDEO = "video"
    VIDEO_RAW = "video_raw"
    VIDEO_JPEG = "video_jpeg"


@dataclass(frozen=True)
class Action6FileFormat:
    kind: str
    format_id: str
    suffix: str
    display_name: str
    min_count: int = 1
    exact_count: int | None = None
    min_size_bytes: int = 1
    require_pairing: bool = False


ACTION6_FILE_FORMATS = MappingProxyType({
    "mp4": Action6FileFormat("video", "mp4", ".mp4", "MP4 视频"),
    "dng": Action6FileFormat("photo", "dng", ".dng", "DNG 照片"),
    "jpeg": Action6FileFormat("photo", "jpeg", ".jpg", "JPEG 照片"),
})

ACTION6_OUTPUT_FORMATS = MappingProxyType({
    OutputSelection.VIDEO: ("mp4",),
    OutputSelection.VIDEO_RAW: ("mp4", "dng"),
    OutputSelection.VIDEO_JPEG: ("mp4", "jpeg"),
})

_ACTION6_FORMATS_BY_SUFFIX = MappingProxyType({
    file_format.suffix.casefold(): file_format
    for file_format in ACTION6_FILE_FORMATS.values()
})


def product_rules_for_output(outputs: str) -> tuple[dict[str, object], ...]:
    """为已明确选择的输出生成相应的最低原片规则。"""
    try:
        selected = OutputSelection(outputs)
        format_ids = ACTION6_OUTPUT_FORMATS[selected]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Action6 原生延时必须选择有效的 outputs") from error
    return tuple({
        "kind": file_format.kind,
        "format_id": file_format.format_id,
        "min_count": file_format.min_count,
        "exact_count": file_format.exact_count,
        "min_size_bytes": file_format.min_size_bytes,
        "require_pairing": file_format.require_pairing,
    } for format_id in format_ids
        for file_format in (ACTION6_FILE_FORMATS[format_id],))


def format_for_suffix(suffix: str) -> Action6FileFormat | None:
    """按不区分大小写的完整后缀取得 Action6 格式。"""
    if not isinstance(suffix, str):
        return None
    return _ACTION6_FORMATS_BY_SUFFIX.get(suffix.casefold())


def output_requirements_description() -> str:
    """从原片映射生成能力说明中的各输出最低要求。"""
    selections = []
    for output, format_ids in ACTION6_OUTPUT_FORMATS.items():
        requirements = []
        for format_id in format_ids:
            file_format = ACTION6_FILE_FORMATS[format_id]
            noun = "个" if file_format.kind == "video" else "张"
            requirements.append(
                f"至少 {file_format.min_count}{noun}非空{file_format.display_name}")
        selections.append(f"{output.value}：" + "和".join(requirements))
    return "；".join(selections)
