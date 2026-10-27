"""业务日志链接入 run/submit 会话的装配集成测试。

真实 build_runtime 组装文件通道与接纳通道：会话失败事实经异步入
口记 ERROR 并落盘；会话结束有序关闭日志组件；未执行会话时
close_runtime 兜底关闭且不补写摘要。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import (
    build_runtime, close_runtime, execute_command,
)
from camctl.logging_runtime.lifecycle import close_logging
from camctl.logging_runtime.models import LogLevel, LogWriteResult
from camctl.logging_runtime.service import LogRecord
from camctl.persistence.initialization import InitOutcome, initialize_state
from integration.logging_runtime.sync_support import FileSyncProbe

from .test_composition import (
    RecordingCatalog, RecordingNotifier, _config_for, _parsed, _plan_body,
)


def _log_text(home: Path) -> str:
    log_file = home / "camctl.log"
    if not log_file.exists():
        return ""
    return log_file.read_text(encoding="utf-8", errors="replace")


class TestSessionLogging:
    async def test_submit_info_stages_are_written_before_channel_closes(self, tmp_path: Path) -> None:
        cfg = _config_for(tmp_path)
        cfg = replace(cfg, log=replace(cfg.log, level="INFO"))
        assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        deps = build_runtime(CommandMode.SUBMIT, cfg,
            catalog=RecordingCatalog(), notifier=RecordingNotifier())
        try:
            outcome = await execute_command(deps, await _parsed(tmp_path, _plan_body()))
            assert outcome.succeeded
            assert not deps.log_runtime.channel.listener_alive
            records = [json.loads(line) for line in _log_text(tmp_path).splitlines()]
            important = [record for record in records if record["stage"] in (
                "command", "session", "session.cleanup")]
            assert [(record["stage"], record["event"]) for record in important] == [
                ("command", "started"), ("session", "started"), ("session", "returned"),
                ("session.cleanup", "started"), ("session.cleanup", "returned"),
                ("command", "result")]
            assert all(record["level"] == "INFO" and record["pid"] == os.getpid()
                and record["session_key"] == deps.session_key for record in important)
            assert all(datetime.fromisoformat(record["logged_at_utc"]).utcoffset().total_seconds() == 0
                for record in important)
            assert not any(record.get("event") == "timing" for record in records)
            assert important[0]["logger"] == "camctl.bootstrap.lifecycle"
            assert "submit" in important[0]["message"]
            assert important[-1]["business_succeeded"] is True
            assert "succeeded" not in important[-1]
        finally:
            close_runtime(deps)

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


@pytest.mark.parametrize("sync_kind", ["file", "directory"])
async def test_runtime_sync_failure_preserves_append_without_file_success(
        tmp_path: Path, monkeypatch, capsys, sync_kind):
    cfg = _config_for(tmp_path)
    assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.SUBMIT, cfg, catalog=RecordingCatalog(),
                         notifier=RecordingNotifier())
    fallback = []
    probe = FileSyncProbe(monkeypatch, Path(cfg.paths.log_file), fail=sync_kind)
    receipt = deps.log_runtime.channel.submit(LogRecord(
        LogLevel.ERROR, "runtime-sync-failure", fallback=fallback.append))

    async def observe():
        return await receipt

    waiter = asyncio.create_task(observe())
    try:
        done, _ = await asyncio.wait({waiter}, timeout=5)
        assert waiter in done, "生产日志装配未结算同步失败回执"
        result = waiter.result()
        assert result.delivered is False
        assert result.appended is True and result.durable is False
        assert result.fallback_written is True
        assert probe.events.count((sync_kind, "started")) == 1
        assert (sync_kind, "completed") not in probe.events
        records = [json.loads(line) for line in _log_text(tmp_path).splitlines()]
        assert [record["message"] for record in records] == ["runtime-sync-failure"]
        assert len(fallback) == 1 and "runtime-sync-failure" in fallback[0]
        diagnostic = capsys.readouterr().err
        assert f"stage={sync_kind}_sync" in diagnostic
        assert "appended=true" in diagnostic and "durable=false" in diagnostic
        assert f"controlled original {sync_kind} sync failure" in diagnostic
        outcome = await execute_command(deps, await _parsed(tmp_path, _plan_body()))
        assert outcome.succeeded is True
    finally:
        if not waiter.done():
            waiter.cancel("test cleanup")
            done, _ = await asyncio.wait({waiter}, timeout=1)
            assert waiter in done
        await asyncio.gather(waiter, return_exceptions=True)
        close_runtime(deps)


async def test_runtime_writer_keeps_original_control_when_write_facts_are_read_only(
        tmp_path: Path, monkeypatch):
    cfg = _config_for(tmp_path)
    assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.SUBMIT, cfg, catalog=RecordingCatalog())
    FileSyncProbe(monkeypatch, Path(cfg.paths.log_file), fail="file")

    class ReadOnlyControl(asyncio.CancelledError):
        @property
        def write_result(self):
            return LogWriteResult(appended=True, durable=False)

    original = ReadOnlyControl("original stderr control")

    class FailedStderr:
        def write(self, message):
            raise original

        def flush(self):
            raise original

    async def observe():
        return await deps.log_runtime.channel.submit(LogRecord(
            LogLevel.ERROR, "readonly-control-record"))

    async def close_and_capture():
        try:
            await close_logging(deps.log_runtime)
        except BaseException as error:
            return error
        pytest.fail("生产装配关闭没有交付原控制异常")

    previous_stderr = sys.stderr
    monkeypatch.setattr(sys, "stderr", FailedStderr())
    waiter = asyncio.create_task(observe())
    closing = None
    try:
        done, _ = await asyncio.wait({waiter}, timeout=5)
        assert waiter in done
        result = waiter.result()
        closing = asyncio.create_task(close_and_capture())
        done, _ = await asyncio.wait({closing}, timeout=5)
        assert closing in done
        control = closing.result()
    finally:
        monkeypatch.setattr(sys, "stderr", previous_stderr)
        if closing is None:
            closing = asyncio.create_task(close_and_capture())
        done, pending = await asyncio.wait({waiter, closing}, timeout=5)
        for task in pending:
            task.cancel("test cleanup")
        if pending:
            later, pending = await asyncio.wait(pending, timeout=1)
            done.update(later)
        await asyncio.gather(*done, return_exceptions=True)
        assert not pending
        close_runtime(deps)
    assert control is original
    assert result.delivered is False and result.appended is True and result.durable is False
    assert result.fallback_written is False
    records = [json.loads(line) for line in _log_text(tmp_path).splitlines()]
    assert [record["message"] for record in records] == ["readonly-control-record"]
