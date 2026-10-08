"""按命令装配静态资源：配置适配、能力目录与 describe。

describe 只读取本地配置与静态能力目录，不创建状态库、会话或调
度器；序列化与 stdout 写入由 CLI 结果通道承担。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields, replace
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
        config = load_config(document, self._defaults)
        paths = {
            field.name: self._expand(getattr(config.paths, field.name))
            for field in fields(config.paths)
        }
        return replace(config, paths=replace(config.paths, **paths))

    def _expand(self, raw: str) -> str:
        if raw.startswith("$HOME/"):
            return str(Path(str(self._home) + raw[len("$HOME") :]))
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

def _row_exists(connection, sql: str, parameters: tuple = ()) -> bool:
    """查询是否存在至少一行；读取后即释放游标。"""
    from contextlib import closing

    with closing(connection.execute(sql, parameters)) as cursor:
        return cursor.fetchone() is not None


def query_work_facts(connection) -> "WorkFacts":
    """会话责任维度的生产查询。

    各维度直接读取持久化投影：未完成动作含未来 scheduled_at 的
    pending 动作；运行中的报告动作属于报告维度，其同步等待本地
    报告处理，不作为普通未完成动作无限延长会话。仍应推进的有限
    流程以未结束 operation_runs 表达。报告维度以报告覆盖边界区
    分——任何状态的报告边界都是已接手尝试，其后的新变化、仍在
    本地处理中的报告或已发布覆盖但尚未保存的本地完成构成待处
    理变化；最新尝试失败且无新变化时只剩等待新触发的责任。残留
    设备事实、可延后清理与等待 ACK 如实呈现但不构成待处理工作。
    """
    from contextlib import closing

    from camctl.session.work import WorkFacts

    def scalar(sql: str) -> int:
        with closing(connection.execute(sql)) as cursor:
            return int(cursor.fetchone()[0])

    unfinished = scalar(
        "SELECT COUNT(*) FROM actions"
        " WHERE status = 1 OR (status = 2 AND type <> 7)")
    settlements = scalar(
        "SELECT COUNT(*) FROM operation_runs WHERE status IN (1, 2)")
    acknowledged = scalar(
        "SELECT acknowledged_wm FROM runtime_state WHERE id = 1")
    # 已接手边界：任意状态报告的最大覆盖水位；失败尝试也覆盖其范围，
    # 之后的新变化才再次构成待处理报告责任。
    attempted = scalar("SELECT COALESCE(MAX(to_wm), 0) FROM reports")
    covered = scalar(
        "SELECT COALESCE(MAX(to_wm), 0) FROM reports WHERE status = 4")
    has_new_changes = _row_exists(
        connection,
        "SELECT 1 FROM report_entity_changes WHERE change_seq > ? LIMIT 1",
        (attempted,))
    in_flight = _row_exists(
        connection,
        "SELECT 1 FROM reports WHERE status IN (1, 2, 3) LIMIT 1")
    # 已发布报告覆盖开始历史但本地完成尚未保存：责任仍开放，会话
    # 继续驱动保存，不把未保存的本地处理解释为已完成。
    unsettled_local = _row_exists(
        connection,
        "SELECT 1 FROM state_syncs s WHERE s.status = 1"
        " AND s.local_report_id IS NULL AND s.action_id IN"
        " (SELECT id FROM actions WHERE status = 2)"
        " AND EXISTS(SELECT 1 FROM reports r WHERE r.status = 4"
        " AND r.from_wm <= s.from_wm"
        " AND r.frozen_event_id >= s.started_boundary_event_id) LIMIT 1")
    failed_uncovered = _row_exists(
        connection,
        "SELECT 1 FROM reports r WHERE r.status = 5 AND r.to_wm = ?"
        " AND NOT EXISTS(SELECT 1 FROM reports p WHERE p.status = 4"
        " AND p.to_wm >= r.to_wm) LIMIT 1",
        (attempted,))
    return WorkFacts(
        unfinished_actions=unfinished,
        required_settlements=settlements,
        pending_report_changes=has_new_changes or in_flight or unsettled_local,
        report_failed_no_new_changes=(not has_new_changes) and failed_uncovered,
        residual_device_facts=_row_exists(
            connection,
            "SELECT 1 FROM device_activities da JOIN actions a ON a.id = da.action_id"
            " WHERE a.status IN (3, 4, 5, 6) AND da.activity_state IN (1, 2) LIMIT 1"),
        deferred_work_cleanup=_row_exists(
            connection,
            "SELECT 1 FROM intermediate_files"
            " WHERE retention_state = 2 AND cleanup_state <> 4 LIMIT 1"),
        waiting_acknowledgement=covered > acknowledged,
        # 快照维护（H6）尚未接入生产数据路径；接入后按维护进度查询。
        snapshot_backlog=False,
    )
