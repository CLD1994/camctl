"""两款 ADB 相机共用的延时时间参数；不声明命令或设备完成能力。"""

TIMELAPSE_INTERVALS_S = (
    1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40,
    60, 120, 300, 1800, 3600,
)
TIMELAPSE_DURATIONS_S = (300, 600, 1200, 1800, 3600, 7200, 10800, 18000)


def time_properties() -> dict:
    """返回独立的时间字段 Schema，数值保持精确秒数。"""
    return {
        "interval_s": {"type": "number", "enum": list(TIMELAPSE_INTERVALS_S)},
        "duration_s": {"type": "integer", "enum": list(TIMELAPSE_DURATIONS_S)},
    }


def time_conditions() -> list[dict]:
    """按有限时长生成标准条件，要求采集时长严格大于间隔。"""
    return [
        {"if": {"required": ["duration_s"],
                "properties": {"duration_s": {"const": duration}}},
         "then": {"properties": {"interval_s": {"exclusiveMaximum": duration}}}}
        for duration in TIMELAPSE_DURATIONS_S
    ]
