"""真实命令装配、SQLite 与文件日志中的业务错误交付。"""

import json
import logging
from pathlib import Path

import pytest

from camctl.acceptance.input import InputReadFailure, InputStage, parse_input, read_input
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import InitOutcome, initialize_state

from .test_composition import RecordingCatalog, RecordingNotifier, _config_for, _parsed, _plan_body


@pytest.mark.asyncio
async def test_handled_input_error_is_logged_when_submit_succeeds(tmp_path):
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    original = OSError(5, "原始输入读取失败", "/original/source")

    class Reader:
        def read(self, _path):
            raise InputReadFailure(InputStage.READ, original) from original

    source = parse_input(await read_input("/incoming/plan.json", Reader()))
    deps = build_runtime(CommandMode.SUBMIT, config, catalog=RecordingCatalog(), notifier=RecordingNotifier())
    try:
        result = await execute_command(deps, source)
        assert result.succeeded
    finally:
        close_runtime(deps)
    text = Path(config.paths.log_file).read_text()
    assert "原始输入读取失败" in text
    assert "/original/source" in text
    assert "/incoming/plan.json" in text
    assert len(text.splitlines()) == 1


@pytest.mark.asyncio
async def test_invalid_action_is_logged_with_saved_identity(tmp_path):
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    body = _plan_body()
    body["actions"][0]["device_id"] = "missing-camera"
    source = await _parsed(tmp_path, body)
    deps = build_runtime(CommandMode.SUBMIT, config, catalog=RecordingCatalog(), notifier=RecordingNotifier())
    try:
        result = await execute_command(deps, source)
        assert result.succeeded
    finally:
        close_runtime(deps)
    text = Path(config.paths.log_file).read_text()
    assert '"action_id":1' in text
    assert '"device_id":null' in text
    assert "missing-camera" in text
    assert len(text.splitlines()) == 1


@pytest.mark.asyncio
async def test_standard_error_is_logged_and_original_handlers_restored(tmp_path):
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.RUN, config, catalog=RecordingCatalog())
    before = tuple(logging.root.handlers)
    original = OSError(5, "第三方完整异常", "/source")

    async def flow(_context):
        try:
            raise original
        except OSError:
            logging.getLogger("library.global_error_witness").error("库调用失败", exc_info=True)

    try:
        # 没有业务待办时会话不会驱动flows；在实际命令主体测试接线。
        from camctl.bootstrap import lifecycle
        from unittest.mock import patch

        async def session(context, _source):
            await flow(context)
            from camctl.session.outcome import SessionOutcome
            return SessionOutcome(True)

        with patch.object(lifecycle, "run_session", session):
            assert (await execute_command(deps, None, flows={})).succeeded
    finally:
        close_runtime(deps)
    assert tuple(logging.root.handlers) == before
    text = Path(config.paths.log_file).read_text()
    assert "库调用失败" in text
    assert "第三方完整异常" in text
    assert '"errno":5' in text
    assert text.count("库调用失败") == 1
