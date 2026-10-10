"""尚待设备契约核实的参数定义。

Schema 从命令候选表取得离散选项。返回定义不自动登记到能力目录；
调用返回含义和延时结束方式尚未核实，不提供可执行任务工厂。
"""
from decimal import Decimal

from camctl.contracts.values import MAX_OBJECT_ID
from camctl.devices.catalog import ActionCapability
from .commands import (ACTION_APERTURE, ACTION_BITRATE, ACTION_EV, ACTION_FOV,
                       ACTION_ISO, ACTION_RESOLUTIONS, ACTION_STABILIZATION,
                       OSMO_EV, OSMO_ISO, TIMELAPSE_PRESETS, CameraModel)


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


def _exposure(iso, ev):
    return {"oneOf": [
        _object({"mode": {"const": "manual"}, "iso": {"type": "integer", "enum": list(iso)}}),
        _object({"mode": {"const": "auto"}, "compensation_ev": {"type": "number", "enum": list(ev)}}),
    ]}


def _record_schema(model: CameraModel, parameter_type: str) -> dict:
    properties = {"type": {"const": parameter_type},
                  "duration_s": {"type": "number", "minimum": Decimal("0.001"),
                                 "maximum": Decimal(f"{MAX_OBJECT_ID // 1000}.{MAX_OBJECT_ID % 1000:03d}"),
                                 "multipleOf": Decimal("0.001")}}
    if model is CameraModel.ACTION6:
        properties.update({"resolution": {"enum": list(ACTION_RESOLUTIONS)},
                           "fov": {"enum": list(ACTION_FOV)},
                           "stabilization": {"enum": list(ACTION_STABILIZATION)},
                           "aperture": {"enum": list(ACTION_APERTURE)},
                           "bitrate": {"enum": list(ACTION_BITRATE)},
                           "exposure": _exposure(ACTION_ISO, ACTION_EV)})
    else:
        properties["exposure"] = _exposure(OSMO_ISO, OSMO_EV)
    required = list(properties)
    if model is CameraModel.ACTION6:
        required.remove("aperture")
        required.remove("bitrate")
    return _object(properties, required)


def _timelapse_schema(model: CameraModel, parameter_type: str) -> dict:
    schema = _object({"type": {"const": parameter_type},
                      "interval_s": {"type": "integer"}, "duration_s": {"type": "integer"},
                      "outputs": {"type": "string"}, "exposure": {"type": "object"}})
    schema["oneOf"] = [
        {"properties": {"interval_s": {"const": preset.interval_s},
                        "duration_s": {"const": preset.duration_s},
                        "outputs": {"const": str(preset.outputs)},
                        "exposure": (_object({"mode": {"const": "manual"}, "iso": {"const": 800}})
                                     if preset.exposure_mode == "manual" else _object({"mode": {"const": "auto"}}))}}
        for preset in TIMELAPSE_PRESETS[model]
    ]
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
    record_description = "手动曝光固定快门 1/60 秒；两种曝光均固定白平衡 5200 K。调用响应待设备核实。"
    if model is CameraModel.ACTION6:
        record_description += "省略 aperture 或 bitrate 时不发送对应设置，保留设备已有值；显式提供时执行对应设置。"
    return (
        ActionCapability("camera_record", record_type, "普通录像候选参数",
                         record_description,
                         False, record, {}),
        ActionCapability("camera_timelapse", timelapse_type, "原生延时摄影候选参数",
                         "仅接受资料给出的完整组合；正常结束及文件写完依据待设备核实。",
                         False, timelapse, {}),
    )
