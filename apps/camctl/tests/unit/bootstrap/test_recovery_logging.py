"""本地恢复诊断保留原调用身份，使用已有非阻塞日志接纳端口。"""

from unittest.mock import create_autospec

from camctl.bootstrap.recovery_logging import recovery_diagnostics_logger
from camctl.capture.recovery import RecoveryBlockedReason, RecoveryDiagnostic
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel


def test_recovery_log_contains_original_attempt_and_actual_blocked_reason():
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    diagnostic = RecoveryDiagnostic(RecoveryBlockedReason.INTENT_OUTSIDE_HORIZON, 41, 3)

    recovery_diagnostics_logger(channel)(diagnostic)

    channel.slog.assert_called_once()
    record = channel.slog.call_args.args[0]
    assert record.level is LogLevel.INFO
    assert "run_id=41" in record.message
    assert "attempt_no=3" in record.message
    assert "reason=intent_outside_horizon" in record.message
    channel.alog.assert_not_called()
