"""默认装配在空数据库上结束取回、清理查询并退出。"""

import asyncio
from pathlib import Path

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import InitOutcome, initialize_state

from .test_composition import RecordingCatalog, _config_for


@pytest.mark.asyncio
async def test_default_run_exits_after_empty_background_discovery(tmp_path):
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.RUN, config, catalog=RecordingCatalog())
    try:
        result = await asyncio.wait_for(execute_command(deps, None), 5)
        assert result.succeeded
        assert all(owner.required_settlements() == 0 for owner in deps.background_flows)
    finally:
        close_runtime(deps)
