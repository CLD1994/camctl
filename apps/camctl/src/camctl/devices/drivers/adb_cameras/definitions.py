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
                       OSMO_BITRATE, CameraModel, FixedSettingRequest,
                       ParameterSettingRequest, VideoModeFacts,
                       fixed_setting_plan_for, fixed_setting_value)
from .action6_formats import OutputSelection, output_requirements_description
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


def _request_text(kind: str, value) -> str:
    if kind == "shutter":
        return f"快门 {value} 秒"
    if kind == "white_balance":
        return f"白平衡 {value} K"
    if kind == "iso":
        return f"ISO {value}"
    if kind == "compensation_ev":
        return f"EV {value}"
    if kind == "bitrate":
        return "高码率" if value == "high" else f"码率 {value}"
    if kind == "video_mode":
        if not isinstance(value, VideoModeFacts):
            raise TypeError("视频模式请求缺少结构化事实")
        panorama = "全景 " if value.panoramic else ""
        description = f"{panorama}{value.resolution}/{value.frame_rate_fps} fps"
        if value.color_profile:
            description += f"、{value.color_profile} 色彩模式"
        return description
    if kind == "color_profile":
        return f"{value} 色彩模式"
    raise ValueError(f"未知的固定设置事实：{kind}")


def _steps_to_task_requests(steps):
    requests = []
    for step in steps:
        if isinstance(step, FixedSettingRequest):
            requests.append((step.kind, step.value, True))
        elif isinstance(step, ParameterSettingRequest) and len(step.choices) == 1:
            kind = "compensation_ev" if step.field == "compensation_ev" else step.field
            requests.append((kind, next(iter(step.choices)), False))
    return requests


def _join_request_text(requests) -> str:
    return "、".join(_request_text(kind, value) for kind, value, _ in requests)


def _request_clause(prefix: str, requests) -> str:
    text = _join_request_text(requests)
    separator = " " if text and text[0].isascii() and text[0].isalpha() else ""
    return prefix + separator + text


def _task_exposure_description(plan) -> str:
    manual = _steps_to_task_requests(plan.exposure["manual"])
    auto = _steps_to_task_requests(plan.exposure["auto"])
    manual_by_fact = {(kind, value): (kind, value, fixed) for kind, value, fixed in manual}
    auto_by_fact = {(kind, value): (kind, value, fixed) for kind, value, fixed in auto}
    common = [item for key, item in manual_by_fact.items() if key in auto_by_fact]
    manual_only = [item for key, item in manual_by_fact.items() if key not in auto_by_fact]
    auto_only = [item for key, item in auto_by_fact.items() if key not in manual_by_fact]
    descriptions = []
    if common:
        descriptions.append(_request_clause("手动和自动两种曝光均请求", common) + "。")
    if manual_only and not auto:
        descriptions.append(_request_clause("仅手动（Manual）曝光请求", manual_only)
                            + "；自动（Auto）曝光不提交 ISO 或 EV 项。")
    elif manual_only:
        facts = _request_clause("", manual_only)
        if all(fixed and kind == "shutter" for kind, _, fixed in manual_only):
            descriptions.append(f"手动曝光请求固定{facts}。")
        else:
            descriptions.append(f"手动（Manual）曝光请求{facts}。")
    if auto_only:
        descriptions.append(_request_clause("自动（Auto）曝光请求", auto_only) + "。")
    return "".join(descriptions)


def _plan_description(plan, action_label: str) -> str:
    common = [step for step in (*plan.before_exposure, *plan.after_exposure)
              if isinstance(step, FixedSettingRequest)]
    description = ""
    if common:
        description += f"{action_label}请求{_join_request_text(_steps_to_task_requests(common))}。"
    description += _task_exposure_description(plan)
    return description


def _schema_branch_description(mode: str, steps) -> str:
    mode_label = "手动" if mode == "manual" else "自动"
    parameter_fields = [
        "ISO 感光度" if step.field == "iso" else "EV 曝光补偿"
        for step in steps if isinstance(step, ParameterSettingRequest)
    ]
    fixed = [step for step in steps if isinstance(step, FixedSettingRequest)]
    parts = []
    if parameter_fields:
        parts.append("填写 " + "和".join(f"{field}请求值" for field in parameter_fields))
    if fixed:
        parts.append(_request_clause("请求", _steps_to_task_requests(fixed)))
    if not parts:
        parts.append("本分支不提交 ISO 或 EV 字段")
    details = "；".join(parts)
    return (f"选择{mode_label}曝光；{details}。固定设置是命令请求，"
            "不代表设备独立读回。")


def _exposure(iso, ev, plan):
    manual = plan.exposure["manual"]
    auto = plan.exposure["auto"]
    return {"oneOf": [
        _object({"mode": _field({"const": "manual"}, "曝光模式",
                                  _schema_branch_description("manual", manual)),
                 "iso": _field({"type": "integer", "enum": list(iso)}, "ISO 感光度",
                               "手动曝光请求值；不代表设备独立读回的 ISO。")}),
        _object({"mode": _field({"const": "auto"}, "曝光模式",
                                  _schema_branch_description("auto", auto)),
                 "compensation_ev": _field({"type": "number", "enum": list(ev)}, "曝光补偿",
                                            "自动曝光补偿请求值，单位 EV；不代表设备独立读回的曝光结果。")}),
    ], "title": "曝光设置",
        "description": "选择手动或自动曝光并填写对应请求值；各分支固定设置见分支说明。固定设置是命令请求，不代表设备独立读回。"}


def _osmo_bitrate_field():
    choices = "；".join(f"{name} 为标称 {setting.nominal_mbps} Mbps"
                       for name, setting in OSMO_BITRATE.items())
    return _field({"type": "string", "enum": list(OSMO_BITRATE)}, "码率档位",
                  f"必填；{choices}。每个拍摄动作开始前动态设置；标称值不代表实测平均码率。")


def _osmo_exposure(plan):
    return {"oneOf": [
        _object({"mode": _field({"const": mode}, "曝光模式",
                                _schema_branch_description(mode, steps))})
        for mode, steps in plan.exposure.items()
    ], "title": "曝光设置", "description": "选择手动或自动曝光；固定请求值见分支说明。"}


def _record_schema(model: CameraModel, parameter_type: str) -> dict:
    plan = fixed_setting_plan_for(model, "camera_record")
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
                                              "必填，光圈值以 f 值表示；请求对应设置。未知响应或调用失败会保存在附属调用结果中，但不阻断本次普通录像的后续设置和启动；不代表设备独立读回的实际光圈值。"),
                           "bitrate": _field({"enum": list(ACTION_BITRATE)}, "码率档位",
                                             "必填；每次普通录像启动前发送对应 simulate_device -s bitrate 命令。工具、通道、退出和输出完整性检查通过时记录命令已接受，不代表实际码率独立读回。"),
                           "exposure": _exposure(ACTION_ISO, ACTION_EV, plan)})
    else:
        properties.update(bitrate=_osmo_bitrate_field(), exposure=_osmo_exposure(plan))
    return _object(properties)


def _timelapse_schema(model: CameraModel, parameter_type: str) -> dict:
    plan = fixed_setting_plan_for(model, "camera_timelapse")
    if model is CameraModel.OSMO360II:
        schema = _object({"type": {"const": parameter_type}, **time_properties(model.value),
                          "bitrate": _osmo_bitrate_field(), "exposure": _osmo_exposure(plan)})
        schema["allOf"] = time_conditions(model.value)
        return schema
    manual_iso = fixed_setting_value(model, "camera_timelapse", "manual", "iso")
    outputs = list(OutputSelection)
    schema = _object({"type": {"const": parameter_type},
                      **time_properties(model.value),
                      "outputs": _field({"type": "string", "enum": outputs}, "输出类型",
                                         "选择本任务请求的成片类型；" + output_requirements_description()
                                         + "。实际产物仍按完成契约核实。"),
                      "exposure": {"type": "object", "title": "曝光设置",
                          "description": "选择手动或自动曝光；各分支请求事实如下："
                              + _task_exposure_description(plan)
                              + "固定设置是命令请求，不代表设备独立读回。",
                          "oneOf": [
                              _object({"mode": _field({"const": "manual"}, "曝光模式",
                                                       _schema_branch_description(
                                                           "manual", plan.exposure["manual"])),
                                       "iso": _field({"type": "integer", "const": manual_iso}, "ISO 感光度",
                                                     f"手动分支请求的固定值 {_request_text('iso', manual_iso)}；不代表独立设备读回。")}),
                              _object({"mode": _field({"const": "auto"}, "曝光模式",
                                                       _schema_branch_description(
                                                           "auto", plan.exposure["auto"]))}),
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
    record_plan = fixed_setting_plan_for(model, "camera_record")
    if model is CameraModel.ACTION6:
        record_description = (
            _plan_description(record_plan, "普通录像")
            + "普通录像按固定顺序发送设置命令；D-Log M 由 42 3d 命令请求。光圈和码率字段均必填。"
            + "光圈命令失败或响应未确认时保留完整附属调用结果，并继续后续设置和启动；该结果不表示光圈已生效。"
            + "码率命令完成且工具、通道、退出和输出完整性检查通过时，记录命令已接受，不表示实际码率独立读回。"
        )
    else:
        record_description = (
            _plan_description(record_plan, "普通录像")
            + "这些设置均是设备命令请求，当前任务不提供对应设置的独立读回，不能据此确认实际值。调用响应待设备核实。"
        )
        record_description += "码率档位必填，每个动作开始前动态设置。"
    record_description += _video_estimate_description(record_estimate)
    timelapse_plan = fixed_setting_plan_for(model, "camera_timelapse")
    timelapse_description = _plan_description(timelapse_plan, "原生延时")
    if model is CameraModel.OSMO360II:
        timelapse_description += "固定输出视频；码率档位必填，每个动作开始前动态设置。"
    if model is CameraModel.ACTION6:
        timelapse_description += (
            f"间隔从该型号允许值中选择，最短为 {timelapse_intervals_s(model.value)[0]} 秒；"
            "采集时长从允许值中选择，且必须严格大于间隔；时间选择不改变曝光或输出。"
            f"输出最低要求：{output_requirements_description()}。"
        )
    timelapse_description += (
        "当前任务不提供这些设置的独立读回，不能据此确认实际值。"
        "参数合法不表示已有完整命令映射；正常结束及文件写完依据待设备核实。"
    )
    return (
        ActionCapability("camera_record", record_type, "普通录像候选参数",
                         record_description,
                         False, record, {}, video_size_estimate=record_estimate),
        ActionCapability("camera_timelapse", timelapse_type, "原生延时摄影候选参数",
                         timelapse_description + _video_estimate_description(timelapse_estimate),
                         False, timelapse, {}, video_size_estimate=timelapse_estimate),
    )
