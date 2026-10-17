"""按相机型号提供延时参数，不声明命令或设备完成能力。"""

_TIMELAPSE_INTERVALS_S = (
    1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40,
    60, 120, 300, 1800, 3600,
)
TIMELAPSE_DURATIONS_S = (300, 600, 1200, 1800, 3600, 7200, 10800, 18000)

# 型号值使用字符串，避免参数定义依赖 commands.CameraModel 并形成导入环。
_MINIMUM_INTERVAL_S_BY_MODEL = {
    "dji-action6": 1,
    "dji-osmo360-ii": 2,
}


def timelapse_model_ids() -> tuple[str, ...]:
    """返回具有权威延时时间定义的型号标识。"""
    return tuple(_MINIMUM_INTERVAL_S_BY_MODEL)


def timelapse_intervals_s(model: str) -> tuple[int, ...]:
    """返回型号允许的间隔；未知型号明确拒绝。"""
    try:
        minimum = _MINIMUM_INTERVAL_S_BY_MODEL[model]
    except (KeyError, TypeError) as error:
        raise ValueError(f"没有型号 {model!r} 的延时间隔定义") from error
    return tuple(value for value in _TIMELAPSE_INTERVALS_S if value >= minimum)


def time_properties(model: str) -> dict:
    """返回独立的时间字段 Schema，数值保持精确秒数。"""
    return {
        "interval_s": {"type": "number", "enum": list(timelapse_intervals_s(model))},
        "duration_s": {"type": "integer", "enum": list(TIMELAPSE_DURATIONS_S)},
    }


def time_conditions(model: str) -> list[dict]:
    """按有限时长生成标准条件，要求采集时长严格大于间隔。"""
    timelapse_intervals_s(model)  # 验证型号，即使关系表达式不需要枚举。
    return [
        {"if": {"required": ["duration_s"],
                "properties": {"duration_s": {"const": duration}}},
         "then": {"properties": {"interval_s": {"exclusiveMaximum": duration}}}}
        for duration in TIMELAPSE_DURATIONS_S
    ]
