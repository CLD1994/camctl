"""没有历史或新文件责任的默认会话有限结束。"""

import asyncio

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import initialize_state

from .test_composition import _config_for


@pytest.mark.asyncio
async def test_default_empty_run_does_not_restart_empty_maintenance(tmp_path):
    config = _config_for(tmp_path)
    initialize_state(config, tmp_path / "state.db")
    deps = build_runtime(CommandMode.RUN, config)
    try:
        outcome = await asyncio.wait_for(
            execute_command(deps, None, poll_interval_s=0.001), timeout=2)
        assert outcome.succeeded, outcome
        assert deps.work_files.history.scan.remaining == config.cleanup.work_file_limit_per_run
        assert deps.work_files.required_settlements() == 0
        assert deps.work_files.supervisor.outstanding() == ()
    finally:
        close_runtime(deps)
