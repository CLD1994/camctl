"""业务日志链接入 run/submit 会话的装配集成测试。

真实 build_runtime 组装文件通道与接纳通道：会话失败事实经异步入
口记 ERROR 并落盘；会话结束有序关闭日志组件；未执行会话时
close_runtime 兜底关闭且不补写摘要。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import (
    build_runtime, close_runtime, execute_command,
)
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord
from camctl.persistence.initialization import InitOutcome, initialize_state

from .test_composition import (
    RecordingCatalog, RecordingNotifier, _config_for, _parsed, _plan_body,
)


def _log_text(home: Path) -> str:
    log_file = home / "camctl.log"
    if not log_file.exists():
        return ""
    return log_file.read_text(encoding="utf-8", errors="replace")


class TestSessionLogging:
    async def test_run_failure_error_recorded_and_logging_closed(
        self, tmp_path: Path,
    ) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)
        ).outcome is InitOutcome.CREATED
        # 状态库文件失效：run 会话以 state_db_error 失败退出。
        Path(cfg.paths.state_db).write_bytes(b"not-a-sqlite-database")
        deps = build_runtime(CommandMode.RUN, cfg, catalog=RecordingCatalog())
        try:
            outcome = await execute_command(deps, None)
            assert outcome.succeeded is False
            assert outcome.reason == "state_db_error"
        finally:
            close_runtime(deps)

        # 会话失败事实作为 ERROR 落盘，无丢弃时不产生关闭摘要。
        content = _log_text(tmp_path)
        assert "run 会话失败: state_db_error" in content
        assert "日志关闭摘要" not in content
        # 会话收场已完成有序关闭；兜底关闭不重复处理。
        assert deps.log_runtime.close_attempts == 1
        assert deps.log_runtime.channel.listener_alive is False

    async def test_submit_success_closes_logging_without_error(
        self, tmp_path: Path,
    ) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)
        ).outcome is InitOutcome.CREATED
        deps = build_runtime(
            CommandMode.SUBMIT, cfg,
            catalog=RecordingCatalog(), notifier=RecordingNotifier(),
        )
        try:
            source = await _parsed(tmp_path, _plan_body())
            outcome = await execute_command(deps, source)
            assert outcome.succeeded is True
        finally:
            close_runtime(deps)

        # 成功会话没有失败事实：不写 ERROR，无丢弃不写摘要。
        content = _log_text(tmp_path)
        assert "会话失败" not in content
        assert "日志关闭摘要" not in content
        assert deps.log_runtime.close_attempts == 1
        assert deps.log_runtime.channel.listener_alive is False


class TestCloseRuntimeFallback:
    async def test_fallback_closes_unclosed_logging(self, tmp_path: Path) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)
        ).outcome is InitOutcome.CREATED
        deps = build_runtime(CommandMode.SUBMIT, cfg, catalog=RecordingCatalog())
        assert deps.log_runtime.close_attempts == 0

        close_runtime(deps)
        # 兜底走同步收场：监听线程停止、标记已关闭，不补写摘要。
        assert deps.log_runtime.close_attempts == 1
        assert deps.log_runtime.listener_stopped is True
        assert deps.log_runtime.channel.listener_alive is False
        assert "日志关闭摘要" not in _log_text(tmp_path)

        # 重复兜底不重复处理。
        close_runtime(deps)
        assert deps.log_runtime.close_attempts == 1


async def test_log_config_drives_wiring(tmp_path: Path) -> None:
    """log 配置块驱动接纳通道参数与级别。"""
    cfg = load_config(
        {
            "paths": {
                "state_db": str(tmp_path / "state.db"),
                "log_file": str(tmp_path / "camctl.log"),
                "staging": str(tmp_path / "staging"),
                "ready": str(tmp_path / "ready"),
                "processing": str(tmp_path / "processing"),
            },
            "log": {
                "level": "DEBUG",
                "queue_capacity": 16,
                "queue_low_watermark": 4,
                "queue_high_watermark": 8,
            },
        },
        ConfigDefaults(),
    )
    assert initialize_state(
        cfg, Path(cfg.paths.state_db)
    ).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.SUBMIT, cfg, catalog=RecordingCatalog())
    try:
        assert deps.log_runtime.level is LogLevel.DEBUG
        channel = deps.log_runtime.channel
        # 级别接纳在通道上生效：DEBUG 记录不被配置级别过滤。
        result = channel.slog(
            LogRecord(level=LogLevel.DEBUG, message="装配探针")
        )
        assert result.decision.kind.name == "ACCEPTED"
        await execute_command(
            deps, await _parsed(tmp_path, _plan_body())
        )
    finally:
        close_runtime(deps)
