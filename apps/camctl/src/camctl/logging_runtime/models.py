"""日志运行时的基础类型。"""

from __future__ import annotations

import enum
from dataclasses import dataclass

__all__ = ["LogFileStage", "LogLevel", "LogWriteResult"]


class LogFileStage(str, enum.Enum):
    """日志责任中有限文件阶段的唯一分类。"""

    STARTUP_SWEEP = "startup_sweep"
    STARTUP_SWEEP_UNLINK = "startup_sweep_unlink"
    MARKER_CHECK = "marker_check"
    MARKER_CREATE = "marker_create"
    COPY_WRITE = "copy_write"
    MARKER_PROGRESS_COPIED = "marker_progress_copied"
    COPY_PUBLISH = "copy_publish"
    MARKER_PROGRESS_PUBLISHED = "marker_progress_published"
    COPY_CLEANUP = "copy_cleanup"
    MARKER_RECOVERY_DELETE = "marker_recovery_delete"

    @property
    def is_startup(self) -> bool:
        return self in (self.STARTUP_SWEEP, self.STARTUP_SWEEP_UNLINK)


class LogLevel(enum.Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class LogWriteResult:
    """同一条记录的追加与持久事实；完成要求由记录级别决定。"""

    appended: bool
    durable: bool = False

    def delivered_for(self, level: LogLevel) -> bool:
        return self.appended and (level is not LogLevel.ERROR or self.durable)
