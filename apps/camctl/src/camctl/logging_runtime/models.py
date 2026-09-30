"""日志运行时的基础类型。"""

from __future__ import annotations

import enum

__all__ = ["LogLevel"]


class LogLevel(enum.Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
