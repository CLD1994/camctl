"""内部媒体探测事实的历史表示与 OSV 内容资格。

公共 media 报告只保存检查结论；容器及逐流原始事实单独存于历史
依据，供修复和恢复使用。未知值原样保留，不以容器时长代替逐流时长。
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
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


def osv_media_error(probe: MediaProbe, target_s: Decimal) -> str | None:
    """容器与必要流的每项时长都独立满足目标，未知不能由另一项补足。"""
    try:
        required = osv_streams(probe)
    except ValueError as error:
        return str(error)
    if probe.duration_s is None:
        return "missing_duration: 未取得必要的实际容器时长"
    if probe.duration_s < target_s:
        return "container_too_short: 实际容器时长不足目标"
    if any(stream.duration_s < target_s for stream in required):
        return "stream_too_short: 必要流时长不足目标"
    return None


def osv_output_error(source: MediaProbe, output: MediaProbe, target_s: Decimal) -> str | None:
    """成品容器、视频内容身份、音频集合与逐路最低时长共同成立。"""
    for probe in (source, output):
        error = osv_media_error(probe, target_s)
        if error is not None:
            return error
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
    if probe.error is None and probe.duration_s is not None and probe.duration_s < Decimal(target_duration_ms) / 1000:
        issues.append(ProcessingError("container_too_short", "probe", {
            "actual_duration_s": probe.duration_s, "target_duration_ms": target_duration_ms}))
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


def _saved_source_probes(connection, processing_id: int, *, at_event_id: int | None = None):
    """按唯一处理身份逐条读取原动作历史，不驻留整段历史。"""
    from camctl.contracts.json_values import parse_exact_json
    from camctl.contracts.values import ConsistencyError
    with closing(connection.execute("SELECT action_id FROM recording_processing WHERE id=?",
            (processing_id,))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("原媒体处理记录不存在")
    with closing(connection.execute(
        "SELECT e.body_json FROM entity_event_links l JOIN history_events e ON e.id=l.event_id"
        " WHERE l.entity_type=1 AND l.entity_id=? AND e.event_type=19"
        " AND (? IS NULL OR e.id<=?) ORDER BY e.id DESC",
        (row[0], at_event_id, at_event_id))) as cursor:
        for raw, in cursor:
            body = parse_exact_json(raw)
            if "source_probe" in body["evidence"] and any(
                    item["table"] == "recording_processing" and item["id"] == processing_id
                    for item in body["rows"]):
                try:
                    yield read_probe(body["evidence"]["source_probe"])
                except (TypeError, ValueError) as error:
                    raise ConsistencyError("原输入探测历史无法解释") from error


def saved_source_probe(connection, processing_id: int, *, at_event_id: int | None = None) -> MediaProbe | None:
    with closing(_saved_source_probes(connection, processing_id, at_event_id=at_event_id)) as probes:
        return next(probes, None)


def saved_osv_input_issues(connection, processing_id: int, target_duration_ms: int):
    """原 OSV 身份适用双视频契约，后续观察不抹除已保存的可靠失败。"""
    from camctl.contracts.values import ConsistencyError
    with closing(connection.execute(
        "SELECT d.original_name,f.relative_path FROM recording_processing p"
        " LEFT JOIN device_files d ON d.id=p.source_device_file_id"
        " LEFT JOIN file_copies c ON c.processing_id=p.id"
        " LEFT JOIN intermediate_files f ON f.id=c.target_file_id"
        " WHERE p.id=? LIMIT 2",
        (processing_id,))) as cursor:
        copies = cursor.fetchmany(2)
    if len(copies) > 1:
        raise ConsistencyError("同一原媒体处理存在多个输入副本")
    if not copies or not any(isinstance(path, str) and path.lower().endswith(".osv") for path in copies[0]):
        return ()
    with closing(_saved_source_probes(connection, processing_id)) as probes:
        for probe in probes:
            issues = osv_input_issues(probe, target_duration_ms)
            if issues:
                return issues
    return ()


def require_repair_media_proof(connection, action_id: int, processing_id: int, output_file_id: int,
        *, output_probe: MediaProbe | None = None, saved: bool = False,
        at_event_id: int | None = None) -> None:
    """有界核对原处理、完整 OSV 副本、原探测与本次登记输出的同一资格。

    新成功使用本次实际 output_probe；正式提升使用同一输出的成功历史。
    旧非 OSV 继续原资格。这里只核对已保存事实，不调用媒体工具。
    """
    from camctl.contracts.values import ConsistencyError
    from camctl.contracts.json_values import parse_exact_json
    from camctl.host_files.models import FilePurpose
    from camctl.host_files.paths import validate_relative_file_path
    from camctl.persistence.transaction import row_facts
    def require(condition, message):
        if not condition:
            raise ConsistencyError(message)
    processing = row_facts(connection, "recording_processing", processing_id)
    require(processing is not None and processing["action_id"] == action_id,
        "修复媒体处理不属于原动作")
    action = row_facts(connection, "actions", action_id)
    require(action is not None, "原修复动作不存在")
    source_id = processing["source_device_file_id"]
    source = None if source_id is None else row_facts(connection, "device_files", source_id)
    with closing(connection.execute("SELECT id FROM file_copies WHERE processing_id=? LIMIT 2",
            (processing_id,))) as cursor:
        copy_ids = cursor.fetchmany(2)
    copies = [row_facts(connection, "file_copies", item[0]) for item in copy_ids]
    files = [row_facts(connection, "intermediate_files", item["target_file_id"]) for item in copies]
    is_osv = (source is not None and isinstance(source["original_name"], str)
        and source["original_name"].lower().endswith(".osv")) or any(
        file is not None and file["relative_path"].lower().endswith(".osv") for file in files)
    if not is_osv:
        return
    require(source is not None and source["source_action_id"] == action_id
        and source["role"] == 2 and source["completion_state"] == 3,
        "OSV 修复缺少原动作的完整原片")
    observer = row_facts(connection, "actions", source["observer_action_id"])
    require(observer is not None and all(observer[key] == action[key] for key in ("device_id", "driver_id")),
        "OSV 原片观察与原设备绑定不符")
    require(len(copies) == 1 and files[0] is not None, "OSV 修复必须具有唯一原输入副本")
    copy, input_file = copies[0], files[0]
    require(copy["source_device_file_id"] == source_id and copy["source_intermediate_file_id"] is None
        and copy["delivery_id"] is None and input_file["purpose"] == 2
        and input_file["owner_action_id"] == action_id and input_file["owner_delivery_id"] is None
        and input_file["relative_path"].lower().endswith(".osv"), "OSV 输入副本身份或归属不符")
    require(copy["verification_state"] in (3, 5) and copy["reset_state"] == 1
        and copy["committed_bytes"] == copy["source_size"] == input_file["size_bytes"] == source["size_bytes"]
        and copy["source_size"] > 0 and input_file["sha256"] is not None
        and input_file["sha256"] == copy["target_sha256"], "OSV 输入副本缺少可靠完整字节核验")
    output = row_facts(connection, "intermediate_files", output_file_id)
    require(output is not None and output["purpose"] == 4 and output["owner_action_id"] == action_id
        and output["owner_delivery_id"] is None and output["relative_path"].lower().endswith(".mp4"),
        "OSV 修复输出身份、归属或 MP4 路径不符")
    try:
        validate_relative_file_path(FilePurpose.RECORDING_INPUT, input_file["id"], input_file["relative_path"])
        validate_relative_file_path(FilePurpose.REPAIR_OUTPUT, output_file_id, output["relative_path"])
    except ValueError as error:
        raise ConsistencyError("媒体路径与原文件身份不符") from error
    basis = processing["repair_basis_json"]
    target_ms = basis.get("target_duration_ms") if isinstance(basis, Mapping) else None
    require(type(target_ms) is int and target_ms > 0, "OSV 修复缺少原固定目标")
    for document in (processing["check_basis_json"], action["execution_spec_json"]):
        if isinstance(document, Mapping) and "target_duration_ms" in document:
            require(document["target_duration_ms"] == target_ms, "原修复目标与首次定义不符")
    source_probe = saved_source_probe(connection, processing_id, at_event_id=at_event_id)
    require(source_probe is not None, "OSV 修复缺少原输入探测")
    with closing(_saved_source_probes(connection, processing_id, at_event_id=at_event_id)) as probes:
        require(all(probe == source_probe for probe in probes), "OSV 原输入探测与首次保存的事实冲突")
    registered = False
    saved_probe = None
    with closing(connection.execute(
        "SELECT e.transaction_id,e.body_json FROM entity_event_links l JOIN history_events e ON e.id=l.event_id"
        " WHERE l.entity_type=1 AND l.entity_id=? AND e.event_type=19 AND (? IS NULL OR e.id<=?)"
        " ORDER BY e.id DESC", (action_id, at_event_id, at_event_id))) as cursor:
        for txn, raw in cursor:
            body = parse_exact_json(raw)
            rows = [row for row in body["rows"] if row["table"] == "recording_processing" and row["id"] == processing_id]
            if not rows:
                continue
            values = rows[0]["after"]["values"]
            if (values.get("repair_state") == 5 and values.get("repair_output_file_id") == output_file_id
                    and "output_probe" in body["evidence"] and saved_probe is None):
                saved_probe = read_probe(body["evidence"]["output_probe"])
            if values.get("repair_state") == 4:
                with closing(connection.execute(
                    "SELECT body_json FROM history_events WHERE transaction_id=? AND event_type=26 ORDER BY id LIMIT 2", (txn,))) as created:
                    for created_raw, in created:
                        for row in parse_exact_json(created_raw)["rows"]:
                            if (row["table"] == "intermediate_files" and not row["before"]["exists"]
                                    and row["after"]["values"].get("purpose") == 4):
                                require(row["id"] == output_file_id and row["after"]["values"].get("owner_action_id") == action_id
                                    and row["after"]["values"].get("relative_path") == output["relative_path"],
                                    "修复成品不是原准备登记的输出")
                                registered = True
    require(registered, "OSV 修复缺少原输出准备登记")
    if saved:
        require(processing["repair_state"] == 5 and processing["repair_output_file_id"] == output_file_id,
            "正式修复成品缺少同一处理的成功状态")
        output_probe = saved_probe
    require(output_probe is not None, "OSV 修复缺少本次成品探测")
    error = osv_output_error(source_probe, output_probe, Decimal(target_ms) / 1000)
    require(error is None, f"OSV 修复媒体资格不符: {error}")
