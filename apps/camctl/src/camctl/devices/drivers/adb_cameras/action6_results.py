"""从 Action6 格式声明建立结果页读取器配置。"""

from .action6_formats import ACTION6_FILE_FORMATS
from .bounded_result_reading import ResultFormat, ResultProfile, list_declared_results


_ACTION6_RESULT_PROFILE = ResultProfile(
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
    """列举 Action6 主页原片，并保留可选 LRF 配对诊断。"""
    return await list_declared_results(driver, request, batch, _ACTION6_RESULT_PROFILE)
