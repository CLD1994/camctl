"""正式会话消费轮询配置并创建可打断等待的通知器。"""

from dataclasses import replace
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap import lifecycle
from camctl.persistence.initialization import initialize_state
from camctl.scheduling.notifications import WorkNotifier
from camctl.session.outcome import SessionOutcome

from .test_composition import _config_for

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("explicit", [False, True])
async def test_run_wires_configured_wait_and_preserves_explicit_ports(tmp_path, monkeypatch, explicit):
    config = _config_for(tmp_path)
    config = replace(config, session=replace(config.session,
        plan_poll_interval_ms=173, wakeup_margin_ms=27))
    initialize_state(config, tmp_path / "state.db")
    session = create_autospec(lifecycle.run_session, return_value=SessionOutcome(succeeded=True))
    monkeypatch.setattr(lifecycle, "run_session", session)
    deps = lifecycle.build_runtime(CommandMode.RUN, config)
    wake = WorkNotifier()
    kwargs = {"wake": wake, "poll_interval_s": 0.123} if explicit else {}
    try:
        assert (await lifecycle.execute_command(deps, None, **kwargs)).succeeded
        context = session.call_args.args[0]
        assert isinstance(context.wake, WorkNotifier)
        if explicit:
            assert context.wake is wake
        assert context.poll_interval_s == (0.123 if explicit else 0.173)
        assert context.wakeup_margin_s == 0.027
    finally:
        lifecycle.close_runtime(deps)
