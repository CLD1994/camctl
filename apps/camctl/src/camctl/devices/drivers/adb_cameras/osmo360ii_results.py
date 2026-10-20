"""OSMO 格式声明接入共用的分页与可选 LRF 配对读取。"""

from .bounded_result_reading import ResultFormat, ResultProfile, list_declared_results


_OSMO_RESULT_PROFILE = ResultProfile(
    evidence_version=1,
    formats=(ResultFormat(".osv", "video", "osv"),),
    preview_format=ResultFormat(".lrf", "video", "lrf"),
    paired_original_format_id="osv",
    original_count_field="osv_count",
    preview_count_field="lrf_count",
    completion_error_code="osmo360ii_file_completion_unconfirmed",
    conflict_error_code="osmo360ii_result_conflict")


async def list_osmo360ii_results(driver, request, batch):
    """OSMO 保留当前 v1 结果证据和格式诊断名称。"""
    return await list_declared_results(driver, request, batch, _OSMO_RESULT_PROFILE)
