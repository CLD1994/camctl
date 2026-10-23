"""从 Action6 格式声明建立结果页读取器配置。"""

from dataclasses import replace

from camctl.devices.tasks import VideoResultMode
from .action6_formats import ACTION6_FILE_FORMATS
from .bounded_result_reading import (
    PreviewReadMode, ResultFormat, ResultProfile, list_declared_results,
)


ACTION6_RESULT_PROFILE = ResultProfile(
    evidence_version=2,
    formats=tuple(ResultFormat(file_format.suffix, file_format.kind, file_format.format_id)
        for file_format in ACTION6_FILE_FORMATS.values()),
    preview_format=ResultFormat(".lrf", "video", "lrf"),
    paired_original_format_id="mp4",
    original_count_field="mp4_count",
    preview_count_field="lrf_count",
    completion_error_code="action6_file_completion_unconfirmed",
    conflict_error_code="action6_result_conflict")


async def list_action6_results(driver, request, batch):
    """按冻结模式列举原始分段文件，或读取单文件的可选配对。"""
    try:
        result_mode = VideoResultMode(request.params.get("video_result_mode"))
    except (TypeError, ValueError) as error:
        raise ValueError("Action6 结果读取需要明确有效的视频结果模式") from error
    profile = (replace(ACTION6_RESULT_PROFILE, preview_read_mode=PreviewReadMode.RAW_PAGE_FILES)
               if result_mode is VideoResultMode.SEGMENTED else ACTION6_RESULT_PROFILE)
    return await list_declared_results(driver, request, batch, profile)
