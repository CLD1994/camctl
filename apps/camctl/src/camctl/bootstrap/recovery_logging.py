"""恢复依据不足的本地诊断接入已有日志队列。"""

from collections.abc import Callable

from camctl.capture.recovery import RecoveryDiagnostic
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel, LogRecord


def recovery_diagnostics_logger(
        channel: LogChannel, *, path_unsafe: Callable[[], bool] | None = None,
        fallback: Callable[[str], None] | None = None):
    """返回非阻塞的诊断入口；日志不提供调用结束或恢复许可。"""

    def emit(diagnostic: RecoveryDiagnostic) -> None:
        message = ("恢复依据不足，保留原调用责任。"
                   f" run_id={diagnostic.run_id}"
                   f" attempt_no={diagnostic.attempt_id}"
                   f" reason={diagnostic.reason.value}")
        if path_unsafe is not None and path_unsafe():
            if fallback is not None:
                fallback(message)
            return
        channel.slog(LogRecord(level=LogLevel.INFO, message=message))

    return emit
