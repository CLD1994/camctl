"""内部媒体探测事实的历史表示与 OSV 内容资格。

公共 media 报告只保存检查结论；容器及逐流原始事实单独存于历史
依据，供修复和恢复使用。未知值原样保留，不以容器时长代替逐流时长。
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any, Mapping

from camctl.host_files.media import MediaProbe, MediaStream


def probe_document(probe: MediaProbe) -> dict[str, Any]:
    return {
        "duration_s": probe.duration_s, "error": probe.error,
        "format_name": probe.format_name,
        "streams": [{
            "index": s.index, "codec_type": s.codec_type,
            "codec_name": s.codec_name, "width": s.width, "height": s.height,
            "duration_s": s.duration_s, "attached_pic": s.attached_pic,
        } for s in probe.streams],
    }


def read_probe(document: Mapping[str, Any]) -> MediaProbe:
    """严格读取完整历史事实；缺失的新事实由调用者保留为 None。"""
    def seconds(value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise ValueError("探测时长必须为精确秒数或未知")
        value = Decimal(value)
        if not value.is_finite() or value < 0:
            raise ValueError("探测时长必须为非负有限秒数")
        return value

    def text(value, *, optional=True):
        if value is None and optional:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("探测文本必须为非空字符串或未知")
        return value

    def integer(value, minimum):
        if value is not None and (type(value) is not int or value < minimum):
            raise ValueError("探测索引或尺寸不合法")
        return value

    if not isinstance(document, Mapping) or set(document) != {
            "duration_s", "error", "format_name", "streams"}:
        raise ValueError("原探测事实结构不完整")
    if not isinstance(document["streams"], list):
        raise ValueError("逐流事实必须是列表")
    streams = []
    for s in document["streams"]:
        if not isinstance(s, Mapping) or set(s) != {
                "index", "codec_type", "codec_name", "width", "height",
                "duration_s", "attached_pic"}:
            raise ValueError("原逐流事实结构不完整")
        if s["attached_pic"] is not None and type(s["attached_pic"]) is not bool:
            raise ValueError("附加图片标记必须为布尔或未知")
        streams.append(MediaStream(integer(s["index"], 0),
            text(s["codec_type"], optional=False), text(s["codec_name"]),
            integer(s["width"], 1), integer(s["height"], 1),
            seconds(s["duration_s"]), s["attached_pic"]))
    return MediaProbe(seconds(document["duration_s"]), text(document["error"]),
        tuple(streams), text(document["format_name"]))


def osv_streams(probe: MediaProbe) -> tuple[MediaStream, ...]:
    """取得双主视频及实际音频；必要事实不足时拒绝猜测。"""
    if probe.error is not None:
        raise ValueError(probe.error)
    if probe.format_name is None or "mp4" not in probe.format_name.split(","):
        raise ValueError("container_unconfirmed: 未确认输入为 MP4 家族容器")
    videos = [s for s in probe.streams
        if s.codec_type == "video" and s.attached_pic is False]
    if (len(videos) != 2 or any(s.codec_type == "video" and s.attached_pic is None
                               for s in probe.streams)):
        raise ValueError("streams_unconfirmed: 未确认恰好两路主视频")
    selected = tuple(videos + [s for s in probe.streams if s.codec_type == "audio"])
    for s in selected:
        if (type(s.index) is not int or s.index < 0 or not s.codec_name
                or s.duration_s is None or not s.duration_s.is_finite()
                or s.duration_s < 0):
            raise ValueError("streams_unconfirmed: 缺少必要流的索引、编码或时长")
        if s.codec_type == "video" and (
                type(s.width) is not int or s.width <= 0
                or type(s.height) is not int or s.height <= 0):
            raise ValueError("streams_unconfirmed: 缺少主视频尺寸")
    if len({s.index for s in probe.streams if s.index is not None}) != len(
            [s for s in probe.streams if s.index is not None]):
        raise ValueError("streams_unconfirmed: 流索引不唯一")
    return selected


def osv_short_streams(probe: MediaProbe, target_s: Decimal) -> tuple[MediaStream, ...]:
    """只返回已明确属于必要媒体、且实际不足目标的流。"""
    return tuple(s for s in probe.streams
        if (s.codec_type == "audio" or (s.codec_type == "video" and s.attached_pic is False))
        and s.duration_s is not None and s.duration_s < target_s)


def osv_output_error(source: MediaProbe, output: MediaProbe, target_s: Decimal) -> str | None:
    """成品容器、视频内容身份、音频集合与逐路最低时长共同成立。"""
    try:
        original, actual = osv_streams(source), osv_streams(output)
    except ValueError as error:
        return str(error)
    if any(s.codec_type == "video" and s.attached_pic is True for s in output.streams):
        return "unexpected_cover: 成品包含未选择的附加封面图"

    def identity(s):
        return (s.codec_type, s.codec_name,
                s.width if s.codec_type == "video" else None,
                s.height if s.codec_type == "video" else None)
    if Counter(map(identity, original)) != Counter(map(identity, actual)):
        return "streams_mismatch: 成品视频编码、尺寸或音频集合与输入不符"
    if any(s.duration_s < target_s for s in actual):
        return "stream_too_short: 成品必要流未达到目标时长"
    return None


def saved_output_probe(connection, output_file_id: int) -> MediaProbe | None:
    """取得本输出成功事件保存的实际成品探测，不根据路径推断类型。"""
    from contextlib import closing
    from camctl.contracts.json_values import parse_exact_json
    with closing(connection.execute(
        "SELECT e.body_json FROM intermediate_files f JOIN entity_event_links l"
        " ON l.entity_type=1 AND l.entity_id=f.owner_action_id"
        " JOIN history_events e ON e.id=l.event_id WHERE f.id=? AND e.event_type=19"
        " ORDER BY e.id DESC", (output_file_id,))) as cursor:
        for raw, in cursor:
            body = parse_exact_json(raw)
            if "output_probe" in body["evidence"] and any(
                    row["table"] == "recording_processing"
                    and row["after"]["values"].get("repair_state") == 5
                    and row["after"]["values"].get("repair_output_file_id") == output_file_id
                    for row in body["rows"]):
                return read_probe(body["evidence"]["output_probe"])
    return None


def osv_input_issues(probe: MediaProbe | None, target_duration_ms: int):
    """区分原输入的明确失败与缺少依据；输出失败不参与该判定。"""
    from camctl.capture.processing import ProcessingError
    if probe is None or (probe.error is not None
            and probe.error.split(":", 1)[0] != "no_video_stream"):
        return ()
    issues = [ProcessingError("stream_too_short", "probe", {
        "stream_index": s.index, "kind": s.codec_type,
        "actual_duration_s": s.duration_s, "target_duration_ms": target_duration_ms,
    }) for s in osv_short_streams(probe, Decimal(target_duration_ms) / 1000)]
    videos = [s for s in probe.streams if s.codec_type == "video"]
    explicit_no_video = (probe.error is not None
        and probe.error.split(":", 1)[0] == "no_video_stream")
    if (explicit_no_video or (probe.streams
            and all(s.attached_pic is not None for s in videos)
            and sum(s.attached_pic is False for s in videos) < 2)):
        issues.append(ProcessingError("required_video_stream_missing", "probe", {
            "actual_primary_video_count": sum(s.attached_pic is False for s in videos),
            "required_primary_video_count": 2}))
    return tuple(issues)


def saved_source_probe(connection, processing_id: int) -> MediaProbe | None:
    """按唯一处理身份逐条读取原动作历史；不驻留整段历史或整页文件。"""
    from contextlib import closing
    from camctl.contracts.json_values import parse_exact_json
    from camctl.contracts.values import ConsistencyError
    with closing(connection.execute("SELECT action_id FROM recording_processing WHERE id=?",
            (processing_id,))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("原媒体处理记录不存在")
    with closing(connection.execute(
        "SELECT e.body_json FROM entity_event_links l JOIN history_events e ON e.id=l.event_id"
        " WHERE l.entity_type=1 AND l.entity_id=? AND e.event_type=19 ORDER BY e.id DESC",
        (row[0],))) as cursor:
        for raw, in cursor:
            body = parse_exact_json(raw)
            if "source_probe" in body["evidence"] and any(
                    item["table"] == "recording_processing" and item["id"] == processing_id
                    for item in body["rows"]):
                try:
                    return read_probe(body["evidence"]["source_probe"])
                except (TypeError, ValueError) as error:
                    raise ConsistencyError("原输入探测历史无法解释") from error
    return None


def saved_osv_input_issues(connection, processing_id: int, target_duration_ms: int):
    """只有原正式选择保存的 OSV 输入副本，才采用双视频原输入契约。"""
    from contextlib import closing
    from camctl.contracts.values import ConsistencyError
    with closing(connection.execute(
        "SELECT f.relative_path FROM file_copies c JOIN intermediate_files f ON f.id=c.target_file_id"
        " JOIN recording_processing p ON p.id=c.processing_id"
        " WHERE p.id=? AND c.source_device_file_id=p.source_device_file_id LIMIT 2",
        (processing_id,))) as cursor:
        copies = cursor.fetchmany(2)
    if len(copies) > 1:
        raise ConsistencyError("同一原媒体处理存在多个输入副本")
    if not copies or not copies[0][0].lower().endswith(".osv"):
        return ()
    return osv_input_issues(saved_source_probe(connection, processing_id), target_duration_ms)
