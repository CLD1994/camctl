"""没有历史或新文件责任的默认会话有限结束。"""

import asyncio

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import initialize_state
from camctl.persistence.repositories.outputs import OutputsRepository
from support.repository_ports import repository_port

from .test_composition import _config_for


@pytest.mark.asyncio
async def test_default_empty_run_does_not_restart_empty_maintenance(tmp_path):
    config = _config_for(tmp_path)
    initialize_state(config, tmp_path / "state.db")
    class ObservedOutputs(OutputsRepository):
        def __init__(self):
            self.ceilings = []
            self.saves = []

        def max_cleanup_candidate_id(self, owned):
            result = super().max_cleanup_candidate_id(owned)
            self.ceilings.append(result)
            return result

        def save_cleanup_intent(self, request, key, owned):
            self.saves.append(request)
            return super().save_cleanup_intent(request, key, owned)

        def save_retention_release(self, request, key, owned):
            self.saves.append(request)
            return super().save_retention_release(request, key, owned)

        def save_cleanup_result(self, request, key, owned):
            self.saves.append(request)
            return super().save_cleanup_result(request, key, owned)

        def save_cleanup_checked(self, request, key, owned):
            self.saves.append(request)
            return super().save_cleanup_checked(request, key, owned)

    observed = ObservedOutputs()
    deps = build_runtime(CommandMode.RUN, config)
    try:
        with repository_port(OutputsRepository, observed, (
                "max_cleanup_candidate_id", "save_retention_release", "save_cleanup_intent",
                "save_cleanup_result", "save_cleanup_checked")):
            outcome = await asyncio.wait_for(
                execute_command(deps, None, poll_interval_s=0.001), timeout=2)
        assert outcome.succeeded, outcome
        assert observed.ceilings == [0]
        assert observed.saves == []
    finally:
        close_runtime(deps)
    assert deps.closed
    assert deps.log_runtime.channel.listener_alive is False
