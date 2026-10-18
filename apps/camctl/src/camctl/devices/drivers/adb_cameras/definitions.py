"""尚待设备契约核实的参数定义。

Schema 从权威参数定义取得离散选项。返回定义不自动登记到能力目录；
调用返回含义和延时结束方式尚未核实，不提供可执行任务工厂。
视频估算采用固定假定参考值，说明和元数据从同一份定义生成；
厂商核实后调整本模块的参考值，不改变拍摄参数或完成契约。
"""
from camctl.contracts.values import MAX_OBJECT_ID
from camctl.devices.catalog import ActionCapability
from .commands import (ACTION_APERTURE, ACTION_BITRATE, ACTION_EV, ACTION_FOV,
                       ACTION_ISO, ACTION_RESOLUTIONS, ACTION_STABILIZATION,
                       OSMO_EV, OSMO_ISO, CameraModel, OutputSelection)
from .timelapse_parameters import (time_conditions, time_properties,
                                   timelapse_intervals_s)


_ASSUMED_REFERENCE_BITRATE_MBPS = {
    CameraModel.ACTION6: {"camera_record": 130, "camera_timelapse": 80},
    CameraModel.OSMO360II: {"camera_record": 170, "camera_timelapse": 300},
}
_TIMELAPSE_PLAYBACK_FPS = 30


def _video_estimate(model: CameraModel, action_type: str) -> dict:
    bitrate = _ASSUMED_REFERENCE_BITRATE_MBPS[model][action_type]
    duration = ({"method": "direct",
                 "seconds": {"source": "parameter", "path": "/duration_s"}}
                if action_type == "camera_record" else
                {"method": "timelapse_interval",
                 "capture_seconds": {"source": "parameter", "path": "/duration_s"},
                 "interval_seconds": {"source": "parameter", "path": "/interval_s"},
                 "playback_fps": {"source": "constant", "value": _TIMELAPSE_PLAYBACK_FPS}})
    return {"bitrate_mbps": bitrate, "duration": duration}


def _video_estimate_description(estimate: dict) -> str:
    description = (f"视频大小按固定假定参考码率 {estimate['bitrate_mbps']} Mbps 估算；"
                   "该值只覆盖目标视频，不计入额外预览视频和独立照片。"
                   "参考码率不是实测平均值，也不表示码率设置已经生效；实际码率待厂商核实。")
    if estimate["duration"]["method"] == "timelapse_interval":
        fps = estimate["duration"]["playback_fps"]["value"]
        description += (f"预计每次采集形成一帧成片，成片按每秒 {fps} 帧播放；"
                        "该关系只用于估算成片时长，首尾帧和实际完成仍按拍摄契约判定。")
    return description


def output_scope_for(driver_id: str) -> dict:
    """资料给出的完整目录范围，不包含执行时取得的基准。"""
    model = CameraModel(driver_id)
    directories = ["/mnt/media_rw/emulated/DCIM"]
    if model is CameraModel.ACTION6:
        directories.append("/mnt/media_rw/sd/DCIM")
    return {"directories": directories}


def _object(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def _field(schema, title, description):
    return {"title": title, "description": description, **schema}


def _exposure(iso, ev):
    return {"oneOf": [
        _object({"mode": _field({"const": "manual"}, "曝光模式", "选择手动曝光；本分支要求填写 ISO 感光度请求值。"),
                 "iso": _field({"type": "integer", "enum": list(iso)}, "ISO 感光度",
                               "手动曝光请求值；不代表设备独立读回的 ISO。")}),
        _object({"mode": _field({"const": "auto"}, "曝光模式", "选择自动曝光；本分支要求填写 EV 曝光补偿请求值。"),
                 "compensation_ev": _field({"type": "number", "enum": list(ev)}, "曝光补偿",
                                            "自动曝光补偿请求值，单位 EV；不代表设备独立读回的曝光结果。")}),
    ], "title": "曝光设置",
        "description": "选择手动或自动曝光并填写对应请求值；固定快门、白平衡及读回边界见任务说明。"}


def _record_schema(model: CameraModel, parameter_type: str) -> dict:
    properties = {"type": {"const": parameter_type},
                  "duration_s": _field({"type": "integer", "minimum": 1,
                                         "maximum": MAX_OBJECT_ID // 1000}, "录像时长",
                                       "设备录像目标时长，单位秒；不含任务说明中的启动余量和结果等待时间。")}
    if model is CameraModel.ACTION6:
        properties.update({"resolution": _field({"enum": list(ACTION_RESOLUTIONS)}, "分辨率与帧率",
                                                  "请求的画面分辨率和帧率组合；候选值由设备能力列出。"),
                           "fov": _field({"enum": list(ACTION_FOV)}, "视野范围",
                                         "请求的镜头视野档位；候选值由设备能力列出。"),
                           "stabilization": _field({"enum": list(ACTION_STABILIZATION)}, "防抖模式",
                                                   "请求的稳定模式；候选值由设备能力列出。"),
                           "aperture": _field({"enum": list(ACTION_APERTURE)}, "光圈",
                                              "可选，光圈值以 f 值表示；填写时请求对应设置，省略时不发送光圈调整并保留设备已有值。"),
                           "bitrate": _field({"enum": list(ACTION_BITRATE)}, "码率档位",
                                             "可选；填写时请求对应码率档位，省略时不发送码率设置并保留设备已有值。"),
                           "exposure": _exposure(ACTION_ISO, ACTION_EV)})
    else:
        properties["exposure"] = _exposure(OSMO_ISO, OSMO_EV)
    required = list(properties)
    if model is CameraModel.ACTION6:
        required.remove("aperture")
        required.remove("bitrate")
    return _object(properties, required)


def _timelapse_schema(model: CameraModel, parameter_type: str) -> dict:
    outputs = (list(OutputSelection) if model is CameraModel.ACTION6
               else [OutputSelection.VIDEO])
    schema = _object({"type": {"const": parameter_type},
                      **time_properties(model.value),
                      "outputs": _field({"type": "string", "enum": outputs}, "输出类型",
                                         "选择本任务请求的成片类型；实际产物仍按完成契约核实。"),
                      "exposure": {"type": "object", "title": "曝光设置",
                          "description": "选择手动或自动曝光；本任务只有手动分支请求固定 ISO、快门和白平衡。",
                          "oneOf": [
                              _object({"mode": _field({"const": "manual"}, "曝光模式",
                                                       "选择手动曝光；本分支请求固定 ISO 800。"),
                                       "iso": _field({"type": "integer", "const": 800}, "ISO 感光度",
                                                     "手动分支请求的固定值 ISO 800；不代表独立设备读回。")}),
                              _object({"mode": _field({"const": "auto"}, "曝光模式",
                                                       "选择自动曝光；该分支不提交 ISO 或 EV 字段。")}),
                          ]}})
    schema["allOf"] = time_conditions(model.value)
    return schema


def candidate_capabilities(driver_id: str) -> tuple[ActionCapability, ...]:
    """生成独立候选定义；驱动登记须另行核实完整可用契约。"""
    model = CameraModel(driver_id)
    prefix = "action6" if model is CameraModel.ACTION6 else "osmo360ii"
    record_type, timelapse_type = f"{prefix}_record", f"{prefix}_timelapse"
    record = _record_schema(model, record_type)
    timelapse = _timelapse_schema(model, timelapse_type)
    for schema in (record, timelapse):
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    record_estimate = _video_estimate(model, "camera_record")
    timelapse_estimate = _video_estimate(model, "camera_timelapse")
    if model is CameraModel.ACTION6:
        record_description = ("手动曝光请求固定快门 1/60 秒；手动和自动两种曝光均请求白平衡 5200 K。"
            "这些固定设置是设备命令请求，当前任务不提供对应设置的独立读回，不能据此确认实际值。调用响应待设备核实。")
    else:
        record_description = ("普通录像请求全景 8K/30 fps、D-Log M 色彩模式和高码率。手动（Manual）曝光请求 ISO 800 与 1/60 秒快门，"
            "自动（Auto）曝光请求 EV 0；两种曝光均请求白平衡 5200 K。当前任务不提供这些设置的独立读回，不能据此确认实际值。")
    if model is CameraModel.ACTION6:
        record_description += "aperture 和 bitrate 均可选；显式提供时请求对应设置，省略时不发送该设置并保留设备已有值。"
    record_description += _video_estimate_description(record_estimate)
    if model is CameraModel.ACTION6:
        record_description += "该固定估算适用于所列分辨率和码率档位，省略码率时也使用同一参考值。"
    if model is CameraModel.OSMO360II:
        timelapse_description = ("原生延时请求全景 8K/30 fps。仅手动（Manual）曝光请求 ISO 800、1/60 秒快门和 5200 K 白平衡；"
            "自动（Auto）曝光不提交 ISO 或 EV 项。当前任务不提供这些设置的独立读回，不能据此确认实际值。"
            "参数合法不表示已有完整命令映射；正常结束及文件写完依据待设备核实。")
    else:
        timelapse_description = (f"间隔从该型号允许值中选择，最短为 {timelapse_intervals_s(model.value)[0]} 秒；"
            "采集时长从允许值中选择，且必须严格大于间隔；时间选择不改变曝光或输出。"
            "参数合法不表示已有完整命令映射；正常结束及文件写完依据待设备核实。")
    return (
        ActionCapability("camera_record", record_type, "普通录像候选参数",
                         record_description,
                         False, record, {}, video_size_estimate=record_estimate),
        ActionCapability("camera_timelapse", timelapse_type, "原生延时摄影候选参数",
                         timelapse_description + _video_estimate_description(timelapse_estimate),
                         False, timelapse, {}, video_size_estimate=timelapse_estimate),
    )
