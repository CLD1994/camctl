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
    if config.devices:
        raise ConfigError(
            "设备目录的驱动定义尚未接入（D1），不能导出部分能力说明"
        )
    return dict(catalog.document())
