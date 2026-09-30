"""按命令装配静态资源：配置适配、能力目录与 describe。

describe 只读取本地配置与静态能力目录，不创建状态库、会话或调
度器；序列化与 stdout 写入由 CLI 结果通道承担。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Protocol

from camctl.bootstrap.config import ConfigDefaults, ConfigError, ConfigSnapshot, load_config

__all__ = [
    "CapabilityCatalog",
    "ConfigAdapter",
    "describe",
    "EmptyCapabilityCatalog",
]


class ConfigAdapter:
    """配置文件读取适配：默认文件位置、显式 --config 与 Decimal 小数。"""

    def __init__(self, home: Path, defaults: ConfigDefaults | None = None) -> None:
        self._home = home
        self._defaults = defaults if defaults is not None else ConfigDefaults()

    def default_config_path(self) -> Path:
        return self._home / ".camctl" / "config.toml"

    def state_db_path(self, config: ConfigSnapshot) -> Path:
        return Path(self._expand(config.paths.state_db))

    def load(self, config_path: Path | None) -> ConfigSnapshot:
        path = config_path if config_path is not None else self.default_config_path()
        try:
            with path.open("rb") as handle:
                document = tomllib.load(handle, parse_float=Decimal)
        except FileNotFoundError:
            # 文件不存在：使用完整默认值，不再寻找另一份文件。
            document = None
        except OSError as error:
            raise ConfigError(f"配置文件无法可靠读取: {path}: {error}") from error
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"配置文件解析失败: {path}: {error}") from error
        return load_config(document, self._defaults)

    def _expand(self, raw: str) -> str:
        if raw.startswith("$HOME/"):
            return str(self._home / raw[len("$HOME/") :])
        return raw


class CapabilityCatalog(Protocol):
    """静态设备能力目录：生成完整能力说明文档。"""

    def document(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class EmptyCapabilityCatalog:
    """无设备部署的能力目录。"""

    def document(self) -> Mapping[str, Any]:
        return {"devices": []}


def describe(config: ConfigSnapshot, catalog: CapabilityCatalog) -> Mapping[str, Any]:
    """从生效配置与静态目录生成完整能力说明文档。

    配置声明了设备但驱动目录未接入时按配置错误拒绝，不导出部分
    说明；文档在写入 stdout 前整体通过公共 Schema 校验。
    """
    return dict(catalog.document())

def query_work_facts(connection) -> "WorkFacts":
    """阶段 1 的工作事实查询：未完成动作与业务水位缺口。

    设备收场、报告失败等待等维度随所属模块接入后补充查询；未知
    维度尚未产生（False 表示没有该类责任，与无法判断区分）。
    """
    from camctl.session.work import WorkFacts

    unfinished = int(
        connection.execute("SELECT COUNT(*) FROM actions WHERE status IN (1, 2)").fetchone()[0]
    )
    acknowledged = int(
        connection.execute("SELECT acknowledged_wm FROM runtime_state WHERE id = 1").fetchone()[0]
    )
    pending_report_row = connection.execute(
        "SELECT EXISTS(SELECT 1 FROM report_entity_changes WHERE change_seq > ?)",
        (acknowledged,),
    ).fetchone()
    return WorkFacts(
        unfinished_actions=unfinished,
        required_settlements=0,
        pending_report_changes=bool(pending_report_row[0]),
        report_failed_no_new_changes=False,
        residual_device_facts=False,
        deferred_work_cleanup=False,
        waiting_acknowledgement=acknowledged > 0,
        snapshot_backlog=False,
    )

class ConfigCapabilityCatalog:
    """按本地设备声明构建的静态目录（D1 真实驱动定义落地前的如实占位）。

    声明的设备即受支持；拍摄参数规则使用开放对象 Schema，不发明
    驱动约束；驱动身份取自声明的 driver 字段。
    """

    _OPEN_SCHEMA = {"type": "object"}
    _CAMERA_TYPES = frozenset(
        {"camera_take_photo", "camera_record", "camera_timelapse"}
    ) | {
        "obtain_action_outputs",
        "delete_action_outputs",
        "cancel_task",
        "report_status",
    }

    def __init__(self, devices: Mapping[str, Any]) -> None:
        self._devices = devices

    def action_types(self) -> frozenset[str]:
        return frozenset(self._CAMERA_TYPES)

    def device_exists(self, device_id: str) -> bool:
        return device_id in self._devices

    def driver_id(self, device_id: str) -> str | None:
        declaration = self._devices.get(device_id)
        if not isinstance(declaration, Mapping):
            return None
        return declaration.get("driver")

    def parameter_definition(self, device_id: str, action_type: str):
        if not self.device_exists(device_id) or not action_type.startswith("camera_"):
            return None
        from camctl.acceptance.ports import ParameterDefinition

        return ParameterDefinition(schema=dict(self._OPEN_SCHEMA), defaults={})

