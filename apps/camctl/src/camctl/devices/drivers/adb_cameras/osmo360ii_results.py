"""OSMO 格式声明接入共用的原始文件分页读取。"""

from dataclasses import replace

from camctl.devices.tasks import VideoResultMode
from .bounded_result_reading import PreviewReadMode, ResultFormat, ResultProfile, list_declared_results


_OSMO_RESULT_PROFILE = ResultProfile(
    evidence_version=1,
    formats=(ResultFormat(".osv", "video", "osv"),),
    preview_format=ResultFormat(".lrf", "video", "lrf"),
    paired_original_format_id="osv",
    original_count_field="osv_count",
    preview_count_field="lrf_count",
    completion_error_code="osmo360ii_file_completion_unconfirmed",
    conflict_error_code="osmo360ii_result_conflict",
    preview_read_mode=PreviewReadMode.RAW_PAGE_FILES)


async def list_osmo360ii_results(driver, request, batch):
    """OSMO 每次返回主目录一页内实际观察到的 OSV/LRF 原始文件。"""
    try:
        result_mode = VideoResultMode(request.params.get("video_result_mode"))
    except (TypeError, ValueError) as error:
        raise ValueError("OSMO 结果读取需要明确有效的 video result mode") from error
    profile = (_OSMO_RESULT_PROFILE if result_mode is VideoResultMode.SEGMENTED else
        replace(_OSMO_RESULT_PROFILE, preview_read_mode=PreviewReadMode.PAIR_FROM_SCOPE))
    return await list_declared_results(driver, request, batch, profile)
