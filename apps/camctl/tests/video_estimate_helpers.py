"""视频大小估算测试的纯内存驱动定义。"""

from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.devices.catalog import (
    ActionCapability,
    DriverDefinition,
    DriverDefinitions,
    build_catalog,
)


def unavailable_task_factory(params):
    raise AssertionError("能力导出不能构造设备执行任务")


def record_capability(estimate):
    return ActionCapability(
        action_type="camera_record",
        parameter_type="estimate_record",
        name="演示录像",
        description="虚构任务，参考码率来自同源声明。",
        preview_supported=False,
        schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"type": {"const": "estimate_record"}},
            "required": ["type"],
            "additionalProperties": False,
        },
        defaults={},
        task_factory=unavailable_task_factory,
        video_size_estimate=estimate,
    )


def record_catalog(estimate):
    return catalog_for_capabilities(record_capability(estimate))


def catalog_for_capabilities(*capabilities):
    actions = {}
    for capability in capabilities:
        actions.setdefault(capability.action_type, []).append(capability)
    config = load_config({"devices": {
        "cam-1": {"kind": "camera", "driver": "estimate_demo"},
    }}, ConfigDefaults())
    return build_catalog(config, DriverDefinitions({
        "estimate_demo": DriverDefinition("estimate_demo", {
            action_type: tuple(items) for action_type, items in actions.items()
        }),
    }))
