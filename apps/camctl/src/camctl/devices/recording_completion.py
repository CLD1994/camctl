"""停止确认后有限等待的事实格式；驱动与恢复共同消费。"""

from collections.abc import Mapping
from enum import StrEnum


class FileCompletionMethod(StrEnum):
    STOP_RETURN_AND_WAIT = "stop_return_and_wait"


def is_completed_file_wait(data, *, activity_id, stop_result_event_id, required_wait_ms):
    """只有原活动、原停止与固定等待规则全部匹配，才可复用原事实。"""
    fields = {"method", "version", "activity_id", "stop_result_event_id",
              "required_wait_ms", "observed_wait_ns", "completed"}
    return (isinstance(data, Mapping) and set(data) == fields
            and data["method"] == FileCompletionMethod.STOP_RETURN_AND_WAIT
            and type(data["version"]) is int and data["version"] == 1
            and data["activity_id"] == activity_id
            and type(data["stop_result_event_id"]) is int
            and data["stop_result_event_id"] == stop_result_event_id
            and type(data["required_wait_ms"]) is int
            and data["required_wait_ms"] == required_wait_ms and required_wait_ms > 0
            and type(data["observed_wait_ns"]) is int
            and data["observed_wait_ns"] >= required_wait_ms * 1_000_000
            and data["completed"] is True)


def completed_file_wait_source(data, *, page_event_id, activity_id,
        stop_result_event_id, required_wait_ms, source_page=None):
    """返回真正执行等待的可靠页；复制事实必须直接引用原实际等待。"""
    if not isinstance(data, Mapping):
        raise ValueError("等待来源要求完整结果依据")
    if type(page_event_id) is not int or page_event_id <= 0:
        raise ValueError("等待来源要求当前可靠结果页身份")
    completion = data.get("file_completion")
    source_id = data.get("file_completion_source_page_event_id")
    completed = is_completed_file_wait(completion, activity_id=activity_id,
        stop_result_event_id=stop_result_event_id, required_wait_ms=required_wait_ms)
    if source_id is None:
        if not completed:
            return None
        actual_source = page_event_id
    else:
        if type(source_id) is not int or not 0 < source_id < page_event_id:
            raise ValueError("复用等待必须引用更早的正整数结果页身份")
        if not completed:
            raise ValueError("复用等待事实须已完成且匹配原活动、停止与规则")
        if (not isinstance(source_page, Mapping)
                or set(source_page) != {"event_id", "activity_id", "data"}
                or type(source_page["event_id"]) is not int
                or source_page["event_id"] != source_id
                or source_page["activity_id"] != activity_id):
            raise ValueError("等待来源不是同活动的原可靠页")
        original = source_page["data"]
        if not isinstance(original, Mapping) or original.get("file_completion_source_page_event_id") is not None:
            raise ValueError("等待来源页须真正执行等待，不能再次引用复制页")
        original_completion = original.get("file_completion")
        if (not is_completed_file_wait(original_completion, activity_id=activity_id,
                stop_result_event_id=stop_result_event_id, required_wait_ms=required_wait_ms)
                or dict(original_completion) != dict(completion)):
            raise ValueError("复用等待与原可靠页的完整事实不一致")
        actual_source = source_id
    if actual_source <= stop_result_event_id:
        raise ValueError("完成等待的实际来源页必须晚于原停止结果")
    return actual_source
